# Curated Sources: Referencing Other People's Content in LEM, With Credit

Research and design note, 2026-10-07. Read-only investigation. Repo reference: `main-run` worktree.
Committed as the research record; what phase 1 actually built, and the owner decisions that
settled §4(h), are in [curated-sources.md](curated-sources.md).
Source quality is marked inline: **[primary]** means official docs, statute or case law, or the
platform's own terms. **[weak]** means vendor blogs, SEO content or secondary summaries. Read [weak]
numbers as directional only.

---

## 0. Bottom line

1. **The safest and cheapest fresh-content path is native: LinkedIn reshare-with-commentary through the
   Posts API.** It needs only `w_member_social`, which LEM already holds. LinkedIn renders and credits
   the original author itself, and the parent post must be a `urn:li:share` or `urn:li:ugcPost`, not
   the `urn:li:activity` that the feed scraper sees.
2. **The strongest original-content path is re-charting cited third-party DATA.** Facts are not
   copyrightable, but chart designs are. The engine already draws stat cards and charts in code from
   verified figures (commit `2384b478`, `image_graphics`), so all it needs is a source the figures
   came from and a "Source:" line. Use public-domain government data first (BLS and Census).
3. **Avoid screenshots of other people's posts and graphics as a default treatment.** They can be fair
   use for commentary, but they are case-by-case. Using one as decoration on a commercial account is
   the weak end of that test.
4. **@mentioning a person via the API is effectively unavailable** to a member app. The tool that
   finds person URNs is restricted, and it only covers an org's followers. Mentioning an organization
   works. Person mentions would have to go through the Selenium composer.
5. Every curated draft is **owner-approved**. Nothing auto-publishes in phase 1.

---

## 1. LinkedIn native options

### 1.1 Reshare ("repost with your thoughts") via the Posts API **[primary]**

- **Shape.** `POST /rest/posts` with the normal post body plus `"reshareContext": {"parent":
  "urn:li:share:…"}`. The `root` is read-only and derived by LinkedIn. `commentary` holds our text,
  and the response header `x-restli-id` returns the new post URN.
  ([Posts API, Reshare a post](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api))
- **Permission.** `w_member_social` covers posting "on behalf of an authenticated member". The docs
  list no separate reshare scope (same page, Permissions table). Their example uses an org author. A
  member author is the same body with `urn:li:person:{sub}`. **This needs a live check** before the
  feature relies on it.
- **Restrictions.**
  - The author can block resharing with `isReshareDisabledByAuthor`. That field is readable on GET,
    but reading other people's posts needs `r_member_social`, which is restricted to approved
    partners. So for a member app, a blocked reshare surfaces as a 4xx on create. Treat that as
    "skip", never retry.
  - The parent must be a share or ugcPost URN. **An `urn:li:activity` URN is not accepted as the
    parent.** The feed exposes activity URNs, so the share URN has to be read from the post's own
    data and kept. ([linkedin-skills PR #57](https://github.com/sergebulaev/linkedin-skills/pull/57)
    **[weak, but consistent with the INVALID_URN_TYPE error in the primary docs]**)
  - Only public posts make sense. A connections-only parent will not render for our audience.
- **Rendering.** The original post is embedded under our commentary with the author's name, face and
  link. LinkedIn does the attribution, which is what makes this the cleanest "use their graphic"
  route: their image renders inside their own post frame.
- **LEM today.** `poster.py::share_on_linkedin` still creates posts through the legacy `/ugcPosts`
  endpoint. Reshares need a small versioned `/rest/posts` helper like the document path already uses
  (`_create_document_post_versioned`).

### 1.2 Quote-style reposts
LinkedIn has no separate "quote post" object. "Repost with your thoughts" is the reshare above. A
plain repost without commentary is the instant variant, and it reaches far fewer people (see §1.4).

### 1.3 @Mentions **[primary]**
- **Format.** Commentary uses the "little" text format: `@[Display Name](urn:li:person:ID)` or
  `@[Org Name](urn:li:organization:ID)`.
  - An org mention must match the full name, case-sensitive.
  - A person mention must match the first name, the last name or both.
  - Reserved characters must be escaped everywhere in commentary: `| { } @ [ ] ( ) < > # \ * _ ~`.
    **This matters for every API post LEM already makes.**
  ([little Text Format](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/little-text-format);
  [Posts API, Mentions](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api))
- **The catch is getting the URN.** The only documented person-URN lookup is the People Typeahead API.
  It is "restricted… granted to select developers only", needs `r_organization_followers`, and
  returns only the **followers of an org page** you administer.
  ([People Typeahead API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-atmention-search-api))
  Third-party posting tools say the same: a person can be mentioned via the API only when they have
  authenticated with your app ([Publora docs](https://docs.publora.com/guides/linkedin-mentions) **[weak]**).
- **Consequence for LEM.**
  - Org mentions via the API are feasible. Org URNs are public in company-page markup.
  - Person mentions need the Selenium composer (`share_composer.py`, which is already used for
    occasion posts) and its typeahead, which brings the anti-bot posture with it.
  - The fallback is the author's name in plain text plus the reshare, which credits them natively.

### 1.4 Reach evidence: reshares versus originals
- Most 2025–2026 coverage agrees that **reposts with thoughts behave close to original posts, and
  instant reposts are suppressed.**
  ([Ordinal](https://www.tryordinal.com/blog/how-to-repost-on-linkedin),
  [Gromming](https://gromming.com/blog/linkedin-repost-algorithm),
  [Teampost](https://teampost.ai/blog/original-posts-vs-repost-linkedin): all **[weak]**, vendor
  blogs)
- Quoted figures such as "reposts get 70–90% less engagement" and "30–40% less reach" have no visible
  methodology **[weak]**.
- The best-sourced practitioner dataset, Richard van der Blom's *Algorithm InSights 2025* (1.8M
  posts), puts weight on dwell time, saves and sends, substantive comments, and the first 60 minutes
  ([summary](https://www.writtenlyhub.com/news/linkedin-engagement-down-50-algorithm-insights-report-2025)
  **[weak secondary]**).
- **Implication.** Commentary must be substantive: a take, a number, a disagreement. A one-line "great
  post 👇" is the instant-repost failure mode, and it would also trip LEM's own slop lint.
- **There is also a relationship benefit.** The original author is notified, which pairs naturally
  with the roster and reciprocity lanes.

---

## 2. External sources

### 2.1 Link posts and preview cards
- **The API does not scrape a URL for you.** "The Posts API does not support URL scraping for article
  post creation… API partners must set article fields such as thumbnail, title, and description."
  The thumbnail must be uploaded first through the Images API.
  ([Posts API, Article](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api) **[primary]**)
  So with the API, *we* fetch the publisher's `og:image`, upload it and set the title and
  description. That is ordinary link-preview practice, it reproduces the publisher's own chosen
  preview, and the card links to them. But it is our upload, so keep it to the og:image exactly.
- **Two alternatives:**
  - Pasting the URL in the Selenium composer lets LinkedIn's own unfurler build the card.
  - LEM's legacy `/ugcPosts` `originalUrl` path in `share_on_linkedin`. Whether it unfurls a
    thumbnail has to be checked live.
- **Reach penalty.** The numbers vary by method:

  | Study | Finding |
  |---|---|
  | van der Blom | about 15–19% lower median reach for one link |
  | LinkPost | 448 vs 705 median impressions (−36%), over 358k posts in 2026 |
  | Forbes, Jul 2026 | about 60% |

  Ordinal reports that the penalty falls mostly on **company pages**, and that personal profiles show
  "almost none".
  ([Ordinal link-penalty review](https://www.tryordinal.com/blog/linkedin-link-penalty-study) **[weak, aggregates others]**;
  [Forbes](https://www.forbes.com/sites/jodiecook/2026/07/30/the-linkedin-link-penalty-cutting-your-reach-by-60/) **[weak, paywalled, unverified]**)
- **Link in the first comment.** 2026 reporting says it now carries close to the same penalty
  ("bridge behavior") ([Gromming](https://gromming.com/blog/linkedin-external-links-penalty),
  [Feedboss](https://www.feedboss.ai/blog/do-linkedin-links-reduce-reach) **[weak]**).
- **Recommendation.** Treat link posts as a **minority treatment**. Prefer a native card, either a
  re-chart or a quote card, with the source named in the body. Put the URL in the body only when
  sending the click is the point. Measure this with LEM's experiments framework rather than trusting
  the vendor numbers.

### 2.2 X posts, screenshots, and the law
- **No embed.** LinkedIn has no tweet embed. The options are a link, which unfurls poorly, a
  screenshot, or quoting the text.
- **US fair use.**
  - *Walsh v. Townsquare Media* (S.D.N.Y. 2020) held that a screenshot of an Instagram post used in
    news commentary was fair use. ([Lexology](https://www.lexology.com/library/detail.aspx?g=d67441fa-a650-4ce2-a1dd-f2e91de26e35))
  - *Mic* (2d Cir.) found a screenshot used to identify and criticise an article transformative.
    ([Freedom of the Press](https://freedom.press/issues/fair-use-win-in-screenshot-case-is-a-victory-for-media-reporting/))
  - The common thread: commentary on the thing itself is protected. A screenshot used as decoration
    for a commercial account is the weak case. ([LegalClarity](https://legalclarity.org/are-screenshots-copyrighted-and-can-i-use-them/) **[weak]**)
  - Embedding is not a safe harbour either: *Goldman v. Breitbart* treated an embed as potentially
    infringing. ([Slashdot](https://yro.slashdot.org/story/18/02/16/0451257/federal-judge-says-embedding-a-tweet-can-be-copyright-infringement) **[weak secondary]**)
- **X Terms.** Users license their content to *X*, not to third parties
  ([summary](https://cryptoslate.com/how-the-new-x-terms-of-service-gives-grok-permission-to-use-anything-you-say-forever-with-no-opt-out/) **[weak]**).
  The Jan-2026 ToS forbids using X or Twitter marks without consent
  ([TechCrunch](https://techcrunch.com/2025/12/16/x-updates-its-terms-files-countersuit-to-lay-claim-to-the-twitter-trademark-after-newcomers-challenge/)).
  A screenshot carries the X logo and UI, which is a further reason not to.
- **When a screenshot is acceptable.** Rarely, and owner-approved only:
  - the subject of the post *is* the tweet, for example a CEO's public statement about AI, and we
    are commenting on it;
  - the source is public and not paywalled;
  - the text is short;
  - avatars and other people's replies are cropped out.
- **Default for X instead.** A **quote card**: a short verbatim excerpt plus "— Name (@handle) on X,
  date", rendered in our typography, plus a link if wanted. It carries no logo or UI and is
  transformative alongside commentary.

### 2.3 Licensed and open sources
- **Creative Commons.**
  - CC BY: attribute with **TASL** (Title, Author, Source, License), link both the work and the
    licence, and mark changes ([CC wiki](https://wiki.creativecommons.org/wiki/Recommended_practices_for_attribution) **[primary]**).
  - **CC BY-ND forbids derivatives**, so no cropping or re-styling.
  - **NC is incompatible** with a business account. Exclude it.
- **Pexels.** Free for commercial use, no attribution required. But: no implied endorsement by
  pictured people or brands, nobody shown in a bad light, no redistribution
  ([Pexels license](https://www.pexels.com/license/) **[primary]**). LEM already uses Pexels
  (`utilities/pexels_helper.py`).
- **Unsplash.** Similar terms, plus a ban on compiling photos into a competing service
  ([Unsplash license](https://unsplash.com/license) **[primary]**).
- **Press kits and newsrooms.** Usually licensed "for editorial use", which is fine for commentary.
  Never edit logos. Check each kit's terms.
- **Re-charting data.**
  - *Feist v. Rural* (1991): facts are not copyrightable. Only original selection, arrangement or
    expression is protected ([Justia](https://supreme.justia.com/cases/federal/us/499/340/) **[primary]**).
  - Re-drawing **numbers** in our own design with "Source: Org, Report, Year" is the strong path.
  - Do **not** trace their layout, palette or illustration, and do not lift a whole curated table
    (selection is protectable).
  - Some publishers add contractual limits on top. **Gartner** requires written approval for any
    quote and allows no paraphrase
    ([Gartner quote policy](https://www.gartner.com/en/about/policies/research-docs) **[primary]**).
    **Stanford AI Index** is CC BY-ND
    ([HAI](https://hai.stanford.edu/ai-index/2025-ai-index-report) **[primary]**): sharing an
    unmodified chart with credit is OK, a restyled version is not. Re-charting the underlying public
    figures is still fine.
  - **BLS:** "everything that we publish… is in the public domain" and they ask for a citation
    ([BLS](https://www.bls.gov/bls/linksite.htm) **[primary]**).
- **oEmbed and link previews.** Useful **for discovery and metadata** (title, author_name,
  provider_name, thumbnail_url), not for rendering on LinkedIn.
  - Bluesky is a registered oEmbed provider.
  - Threads moved to "Meta oEmbed Read" in 2025, which requires app review
    ([Bluehost](https://www.bluehost.com/blog/meta-oembed-read-explained/) **[weak]**).
  - YouTube's oEmbed is open.
  - Use oEmbed or `og:` tags to fill the attribution block automatically.

---

## 3. Source platforms evaluated (ranked)

Audience: small-business owners who are curious about practical AI and automation. "Re-chart" means
redrawing the facts in LEM's own design.

| # | Source | Access (API/RSS, cost, limits) | Credit / reuse norms | Fit | Risk | Tier |
|---|---|---|---|---|---|---|
| 1 | **LinkedIn feed and roster** (already read) | Selenium already in place; reshare via `w_member_social` | Native reshare credits automatically | Highest: the audience is already there, and reciprocity follows | Low with a reshare; never screenshot | **1 Core** |
| 2 | **US government data: BLS, Census BTOS, SBA Advocacy** | Free APIs and CSV; BTOS bi-weekly with an AI-use supplement ([Census BTOS](https://www.census.gov/hfp/btos/data_downloads), [SBA Advocacy AI spotlight](https://advocacy.sba.gov/wp-content/uploads/2025/09/Research-Spotlight-AI-in-Business-Small-Firms-Closing-In_-092425.pdf)) | Public domain, cite the agency ([BLS](https://www.bls.gov/bls/linksite.htm)) | Very high: "18% of firms used AI" type figures speak directly to SMB owners ([Census WP](https://www2.census.gov/library/working-papers/2026/adrm/ces/CES-WP-26-25.pdf)) | Very low | **1 Core** (re-chart) |
| 3 | **Official company blogs and press rooms** (OpenAI, Anthropic, Google, Microsoft, Shopify, Intuit, HubSpot) | RSS on most; free | Link and quote short; press-kit images for editorial use; never edit logos | High: tool launches drive "what this means for you" posts | Low; don't imply endorsement | **1 Core** |
| 4 | **Substack and industry newsletters** | Every publication has `/feed` RSS ([WPRSS](https://www.wprssaggregator.com/substack-rss-feed/) **[weak]**); free | RSS is not a republish licence ([example](https://haje.medium.com/copyright-just-because-i-have-an-rss-feed-it-doesnt-mean-you-get-to-steal-my-content-c8ea505a8b07)); link plus short quote | High when curated (operator-focused AI newsletters) | Paywalled posts excluded | **1 Core** |
| 5 | **Analyst and research reports** (McKinsey State of AI, Stanford AI Index, a16z, Gartner) | Web/PDF; Stanford data on Kaggle ([Kaggle](https://www.kaggle.com/datasets/paultimothymooney/ai-index-report-2025)) | Stanford CC BY-ND; **Gartner requires written approval for any quote** ([Gartner](https://www.gartner.com/en/about/policies/research-docs)); McKinsey: cite and re-chart facts, check its ToU (not verified here) | High authority | Gartner contractual risk; restyled ND charts | **1 Core** (re-chart; Gartner excluded unless approved) |
| 6 | **YouTube** | Data API v3, 10,000 units/day free, search = 100 units ([Google docs via summary](https://www.socialcrawl.dev/blog/youtube-data-api-2026) **[weak]**); channel RSS free; oEmbed open | Link post shows their thumbnail; don't re-upload thumbnails or frames ([summary](https://thumbnailtest.com/guides/copyright-strike-youtube-thumbnail/) **[weak]**) | Medium-high (demos, founder interviews) | Low via link | **2 Occasional** |
| 7 | **Podcasts (transcripts)** | `<podcast:transcript>` RSS tag where published ([Podcast namespace](https://github.com/Podcastindex-org/podcast-namespace/blob/main/docs/1.0.md)); Podcast Index API is metadata only ([Podchaser](https://www.podchaser.com/articles/api/podcast-transcript-api) **[weak]**) | Short quote plus show/guest/episode credit | Medium-high (operator interviews) | Misquoting; transcription costs if no tag | **2 Occasional** |
| 8 | **Hacker News** | Official Firebase API, free, no key ([HN API](https://github.com/hackernews/api)) | Link the underlying article, not HN; quoting commenters needs care | Medium: early signal on tools, too technical for SMB as-is | Low | **2 Occasional** (discovery only) |
| 9 | **GitHub releases and trending** | REST 5,000 req/h authenticated ([GitHub docs](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)); releases Atom feed free | Licence is per repository; link, don't copy README images | Low-medium (only for big open-source tool releases) | Low | **2 Occasional** |
| 10 | **arXiv / HF Trending Papers** | arXiv API free; metadata CC0 ([arXiv ToU](https://info.arxiv.org/help/api/tou.html)); **Papers with Code shut down Jul 2025**, now Hugging Face Trending Papers ([Coursera](https://www.coursera.org/articles/papers-with-code) **[weak]**) | Paper text and figures need the holder's permission unless CC licensed; re-chart reported numbers with citation | Low for SMB (needs heavy translation) | Over-claiming preprints | **2 Occasional** |
| 11 | **Bluesky** | AT Protocol, free, public firehose ([Blotato](https://www.blotato.com/blog/bluesky-api-pricing) **[weak]**); oEmbed provider | Quote card plus handle; no screenshots | Medium (AI researchers moved there) | Low | **2 Occasional** |
| 12 | **Mastodon** | Per-account `.rss` and public API ([dramsch.net](https://dramsch.net/today-i-learned/social-media/mastodon-user-post-timeline/) **[weak]**) | Instances take no licence, so users keep full copyright ([Naomi Korn](https://naomikorn.com/2022/11/24/letting-the-bird-out-the-cage-how-does-copyright-and-licensing-work-on-mastodon/)); quote short, attribute | Low for SMB | Low; community norms against commercial reuse | **2 Occasional** |
| 13 | **Medium** | RSS for profiles, publications and tags ([Medium help](https://help.medium.com/hc/en-us/articles/214874118-Using-RSS-feeds-of-profiles-publications-and-topics)) | Link plus short quote; member-only posts are paywalled | Medium, uneven quality | Paywall | **2 Occasional** |
| 14 | **Google Trends** | Official API is an **application-gated alpha** (Jul 2025) ([Google](https://developers.google.com/search/apis/trends)); scraping violates the ToS | "Source: Google Trends" on a re-chart | Medium (timing signal for topics) | Access, not brand | **2 Occasional** (manual, or apply for the alpha) |
| 15 | **Product Hunt** | GraphQL v2; **non-commercial by default**, commercial use by contacting PH ([PH API docs](https://api.producthunt.com/v2/docs)) | Link the maker's page | Medium (SMB tools) | ToS for automated commercial pulls | **2 Occasional** (manual) |
| 16 | **X** | Pay-per-use since Feb 2026: $0.005 per post read; free tier gone ([Postproxy](https://postproxy.dev/blog/x-api-pricing-2026/) **[weak]**) | Quote card with name/@handle; no logo or UI screenshots ([X ToS marks](https://techcrunch.com/2025/12/16/x-updates-its-terms-files-countersuit-to-lay-claim-to-the-twitter-trademark-after-newcomers-challenge/)) | Medium-high (AI leaders post there first) | Moderate: platform politics, toxicity in replies | **2 Occasional** (curated list, owner approval) |
| 17 | **Threads** | Keyword search 500 queries/7 days; oEmbed needs app review ([PPC Land](https://ppc.land/meta-introduces-new-search-and-analytics-features-to-threads-api-platform/) **[weak]**) | Quote card | Low-medium | Low | **3 Avoid for now** (low signal for the access effort) |
| 18 | **Reddit** | Free tier needs pre-approval since Nov 2025 and **forbids commercial use**; commercial use needs a contract ([Octolens](https://octolens.com/blog/reddit-api-pricing) **[weak]**) | Users keep copyright; quoting posters is sensitive | Medium (r/smallbusiness pain points make good *research* material) | ToS for automated commercial use; outing pseudonymous posters | **3 Avoid** automation; manual reading only, paraphrase themes, never quote users |
| 19 | **Truth Social** | No public API; ToS bars any commercial reproduction without written permission ([Truth Social ToS](https://help.truthsocial.com/legal/terms-of-service/)); paid data licence only for financial firms ([Axios](https://www.axios.com/2026/07/16/truth-social-license-data-wall-street)) | No reuse licence | Very low for SMB AI | **High for a nonpartisan B2B brand:** content is overwhelmingly political, and quoting it signals alignment regardless of intent; ToS bars it | **3 Avoid** |

**Political-content guardrail (all sources).** This is a topic filter, not a platform judgement. Drop
any candidate whose text, author or link classifies as electoral or partisan politics, a named
politician, or a culture-war topic. That applies equally to X, Threads, Bluesky and Truth Social. A
government *statistic* passes. A politician's *statement* about AI policy goes to owner review only
and is never auto-selected.

---

## 4. Design: the "curated source" feature

### (a) Source discovery
- **Phase 1 inputs, in order of value per unit of effort:**
  1. LinkedIn feed and roster posts LEM already reads (`app/engagement/feed.py`, roster targets).
  2. An owner-maintained **RSS allowlist** of company blogs, newsrooms, Substack/newsletter `/feed`s
     and YouTube channel feeds.
  3. A fixed **government-data watchlist** (BLS series, Census BTOS AI items, SBA Advocacy).
- **Later:** HN and GitHub as signal, Bluesky lists, X lists (paid reads), podcast transcript tags.
- **Store** each candidate in a new `curated_sources` table with: URL, canonical URL, platform, author
  name and handle, title, og:image URL, `og:site_name`, licence (`cc-by`/`public-domain`/`unknown`/
  …), paywalled flag, LinkedIn share URN (for reshares), discovered_at, relevance score, and status.
  Score with the existing targeting and topic-relevance scorer, plus a freshness decay.

### (b) Treatments, ranked by safety, then engagement

| Rank | Treatment | When | Mechanics |
|---|---|---|---|
| 1 | **Native reshare plus commentary** | The source is a public LinkedIn post we hold a share/ugcPost URN for | `/rest/posts` + `reshareContext.parent`; commentary ≥ N chars with a real take |
| 2 | **Re-charted data card** | The source states figures (gov data, reports, articles) | `image_graphics` stat_card/highlight_chart; each figure validated verbatim against the source sentence (already how #2241 refuses untraceable numbers); "Source: Org, Title, Year" drawn on the card **and** in the body |
| 3 | **Quote card** | A short, quotable line from a named person or publication (X, podcasts, blogs) | Typeset by us; ≤ 40 words and ≤ 25% of the source; "— Name, Role, Outlet, date"; no logos, avatars or platform UI |
| 4 | **Link post with the publisher's preview** | The goal is the click (a must-read article) | Article content; thumbnail = og:image re-uploaded unmodified, plus title and description from og tags; body names the outlet and author |
| 5 | **@mention of author/org** | An add-on to 1–4 | Org mention via API; person mention only through the Selenium composer or as plain text |
| — | Screenshot | Never automatic | Manual, owner-only, commentary-on-the-thing cases (§2.2) |

### (c) Attribution format
- **Body**, always, on the last line before any hashtags:
  `Source: {Author} — "{Title}", {Outlet}, {Mon YYYY}.` followed by the URL when the treatment is a
  link post. For a CC work, add `({License}, changes: re-charted)`.
- **On-image**, for re-charts and quote cards: a small-type bottom line, `Source: {Outlet}, {Title}
  ({Year})` / `Data: U.S. BLS, {series}`.
- **Never** imply authorship. The commentary uses "{Author} found…" and "{Outlet} reports…", never a
  bare claim. LinkedIn renders reshare credit itself, so don't duplicate it in the body.
- Escape little-text reserved characters in every API commentary.

### (d) Guardrails (enforced in code, failing CLOSED, where violating them is irreversible)
1. **Provenance required.** No candidate can be drafted without a stored URL, author and fetch
   snapshot. Unknown author means skip.
2. **Licence gate.** Allowed: `public-domain`, `cc-by`, `cc-by-sa`, `editorial-press-kit`,
   `facts-only` (re-chart). Exclude `cc-*-nc` and `-nd` for derivatives. `unknown` restricts the
   post to link or reshare treatments, with no image reuse.
3. **Quote limits.** A quote is at most 40 words and at most 25% of the source text. One quote per
   post. Quoted verbatim, with a string match against the snapshot.
4. **No paywalled or private content.** Skip on a paywall marker, a login wall, a members-only
   Medium post, a connections-only LinkedIn post, or a DM.
5. **No people's photos.** Never reuse avatars or photos of identifiable people from sources. Only a
   publisher's own og:image inside a link card. A people-photo og:image falls back to a text-only
   card.
6. **No logos, no platform UI** on anything we render.
7. **Ban lists.** Political/partisan topic filter (§3). Gartner (unless approved). Truth Social and
   Reddit (no automated reuse).
8. **Our own words carry the post.** The commentary must pass the existing slop lint and similarity
   gate, plus a "has a take" check, and must not just restate the source.
9. **Frequency cap.** Curated posts are at most one in three planned posts per week, so the
   account stays original-first.
10. **Takedown path.** One click deletes the post through the existing delete API and marks the
    source `do_not_use`.

### (e) Approval gating
**Yes, every curated draft is owner-approved** (`PostStatus.PENDING` → `APPROVED`), with no auto-publish
in phase 1. This matches LEM's posture for consequential, one-way acts: the DM nurture and
`profile_viewer_dm_auto_send` are OFF by default, and the agent scope "may queue but NEVER approve".
The approval card shows:
- the source preview
- the treatment
- the licence verdict
- the quote-length meter
- the exact attribution line

Consider auto-approval for **gov-data re-charts only** after 30 days with zero owner edits.

### (f) Fit with the existing pipeline
- **PostType.** Keep text/carousel/video for rendering. Add a separate **`posts.source_treatment`**
  ENUM column (`reshare`, `rechart`, `quote_card`, `link`), plus FKs to `curated_sources`. That needs a
  timestamped Flyway migration (db-migration skill).
  - A reshare is a text post with a parent URN.
  - A re-chart or quote card is a single-image post.
  - A link post is article content.
  - Avoid widening `PostType`: every switch on it would need touching.
- **Content plan.** In `POST_DAY_TYPES`, curated posts take an **`authority`** slot in the 70/20/10
  mix (commentary on credible outside evidence), or a `value` slot for a re-charted stat. Never
  `promo`. They don't add slots: they substitute one of the `posts_per_week` slots when a
  high-scoring candidate exists.
- **Image engine.**
  - A re-chart is the `stat_card`/`highlight_chart` archetype with `graphic_facts` coming from the
    source snapshot instead of from our draft.
  - A quote card is a new code-drawn archetype beside `receipt` and `before_after` in
    `image_graphics`. It is not an `image_brief` prompt, so no AI render and no text in an AI
    render, consistent with docs/image-stack.md ("add a preset, never a per-type helper"; "NO
    text/logos in a render prompt").
  - The `lem-vision` gate still applies.
  - The receipt records `source_url` and `license`.
- **Writer.** Reuse `content_framework`/`content_alignment`, and pass the source snapshot as the
  research input (`content_research` already returns `{findings, sources}`), with a curated-commentary
  framework variant.
- **Publisher.**
  - Add a versioned `/rest/posts` helper in `poster.py` for reshare and article-with-thumbnail. The
    member author is `urn:li:person:{sub}`.
  - The OAuth path is untouched by the Selenium 429 breaker.
  - Person @mentions are the only Selenium dependency, and they are deferred.
- **Telemetry.** Add an `EventSpec` for `curated_post_published` with treatment and platform as
  **string** labels. Compare reach per treatment using `post_engagers`/post stats.

### (g) Phased build plan

**Phase 1: reshare and re-chart, owner-approved** (recommended scope)
- **Inputs:**
  - the LinkedIn feed and roster (already read)
  - an RSS allowlist of company blogs, newsrooms and Substack `/feed`s, owner-curated, about 20 feeds
  - a BLS/Census BTOS/SBA watchlist
- **Build:**
  - the `curated_sources` table plus a collector beat
  - a share-URN capture in the feed scraper
  - the `/rest/posts` reshare helper
  - the re-chart treatment through `image_graphics`
  - the attribution formatter and little-text escaper
  - guardrails 1–4 and 7–10
  - SPA approval card
  - a flag `CURATED_SOURCES_ENABLED` (default OFF)
- **Acceptance:**
  1. A unit test builds the reshare body with `reshareContext.parent` as a share/ugcPost URN. An
     activity URN is refused.
  2. One live reshare by user 1 succeeds, and its permalink shows the embedded original. This proves
     member-token reshare.
  3. A re-chart card whose figure is missing from the source snapshot is refused before render.
  4. Every published curated post carries a `Source:` line. A test asserts this for every treatment.
  5. A candidate with a political-topic classification, a paywall flag or an NC licence never
    reaches PENDING.
  6. No curated post publishes without APPROVED status.
  7. At most one in three planned posts per week is curated.

**Phase 2: quote cards and link posts**
- Quote-card archetype, og:image link posts, YouTube channel RSS, podcast transcript tags, and
  Bluesky lists.
- **Acceptance:**
  - A quote over 40 words or 25% of the source is refused.
  - The quote string matches the snapshot verbatim.
  - The link-post thumbnail is byte-identical to the og:image.
  - An A/B test (experiments framework) of link-in-body versus a native card with a source line.

**Phase 3: mentions and signals**
- Org @mentions via the API, person mentions via the composer behind a probe flag, HN/GitHub/X-list
  signal ingestion (X pay-per-use budget cap), and per-treatment reach reporting.
- **Acceptance:**
  - An org-name mention renders as a link in a live post.
  - The X spend cap is enforced.
  - The KPI dashboard shows reach per treatment.

### (h) Open questions for the owner
1. **Feeds.** Which ~20 RSS feeds and newsletters belong on the allowlist? (Who do your buyers
   already trust?)
2. **Reshares.** Are you comfortable resharing *peers and competitors*, or only customers, partners
   and big-brand sources?
3. **Ratio.** Is one curated post in three the right ceiling, or should it be lower, e.g. one per
   week?
4. **X.** Should LEM pay for X reads (pay-per-use, about $0.005 per post), or will you paste X links
   by hand?
5. **Screenshots.** Should they be allowed at all as a manual-only treatment, or banned outright?
6. **Policy.** Should a politician's or regulator's statement about AI policy ever be eligible, if
   owner-approved? Or should the political filter be absolute?
7. **Approval.** Do you want auto-approval for government-data re-charts after a clean trial period?
8. **Gartner/Forrester.** Do you have client access, which affects quote rights? Otherwise they are
   excluded.
9. **Notifications.** Should a reshare also trigger a short heads-up DM or comment to the original
   author, through the existing approval-gated DM lane?

---

## Caveats
- I have not verified live that a member token can create a reshare. The primary docs show an org
  example only. Phase-1 acceptance #2 exists to settle that.
- Reach numbers for reshares and links come from vendor and practitioner studies with opaque
  methods. LEM should measure its own using the experiments framework.
- This is not legal advice. Fair use is fact-specific, which is why screenshots stay manual and
  owner-approved.
- `docs/visual-archetypes-research.md` and `image_graphics` exist in commit `2384b478` (refs #2241).
  They are not on `main-run` yet.
