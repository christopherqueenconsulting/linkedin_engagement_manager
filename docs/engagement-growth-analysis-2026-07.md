# Engagement Growth Analysis — user 1, July 2026

Why engagement is low, what the 2026 LinkedIn landscape rewards, what 20 comparable creators
actually do, and the plan (Milestones 13–14) to fix it. Compiled 2026-07-26 from production data
(MySQL `post_stats`/`logs`/Redis funnel), the van der Blom / AuthoredUp 2025–2026 algorithm
research, LinkedIn's May 2026 AI-slop announcements, and a 20-profile creator study.

## 1. Internal evidence (production data, 2026-06-26 → 2026-07-26)

**Reach is dying, not dead on arrival.** 28 posts. Impressions per post: median ~64, range
28–229 — and declining: first-week posts averaged ~125, last-week posts ~48. LinkedIn granted
initial distribution, engagement didn't follow (11 reactions TOTAL across 28 posts, ~0–1 real
external comments each, 0 reposts, 0 saves, engagement rate ~0.5% vs a ~3.6% B2B benchmark),
and distribution was cut. That negative feedback loop — low-value-to-audience content → no
engagement → reach decay — explains the decline better than 429 history alone (no breaker was
tripped during the sample; past bot-signal history likely adds drag but is not the primary cause).

**The content confirms the owner's own diagnosis.** Nearly every recent post is the same
cost-per-successful-call thesis in consultant framing; CTAs sell the diagnostic call; no real
numbers/artifacts from his actual production system; one post is a generic
blockchain-in-healthcare listicle unrelated to `focus_topics`. Posted daily (28/28 days —
van der Blom 2026: daily posting = −26% reach per post) at scattered hours including 03:00/05:00.

**The comment engine produces the exact slop LinkedIn now demotes.** Sampled comments open with
"I totally agree that…", "You raise an excellent point…", "Spot on—", follow one
[validate]+[generic insight]+[question] template, and near-duplicate each other across posts on
the same day. The feed funnel (Jul 25) examined 21 posts, **0 matched include_topics**, and the
fallback commented on off-ICP posts (AI-in-HR/hiring) — actively harmful under 2026 Topic
Authority ranking, where off-topic commenting damages the account's topical identity.
`include_authors` has never been populated. Volume: ~2 comments/day landing vs the 8/day cap.

**The growth flywheel has no input.** post_engagers: 1 row ever. Reactions given: 2 in 14 days.
Connection requests: 1 ever — and it targeted an existing 1st-degree connection (scraped name
"… Verified Profile 1st" shows degree-badge text polluting the name and no already-connected
check) and failed. `scheduled_dms`: 0 rows ever. `outreach_funnel_targets`: 0 rows. DM caps,
templates, follow-ups, nurture: all idle. Impressions are structurally capped by an audience
that isn't growing.

**Measurement gaps.** No follower-count or profile-view tracking anywhere (audience growth is
invisible); no comment-outcome tracking (did the author reply? is our comment demoted out of
"Most Relevant"?); perf snapshot sums all `post_stats` rows (inflates cumulative impressions);
`post_stats.topic` is never populated.

## 2. External landscape (2025–2026) — what changed

- **Platform-wide reach collapse**: views −50%, engagement −25%, follower growth −59% YoY for
  ~95% of creators (van der Blom, 1.8M posts); median impressions −65% vs 2023. Feed share of
  "other creators" fell 57%→28%. Benchmarks must be judged against this baseline.
- **360Brew semantic ranking** (LinkedIn's 150B decoder-only ranker, feed-deployed Mar 2026):
  ranking reads the post, the author's profile, and history. **Topic Authority** — topical
  consistency between profile, posts, and engagement — is the 2026 distribution currency.
  Hashtag/topic pages are gone (Oct 2024); discovery is semantic + evergreen resurfacing
  ("suggested posts" weeks later), so save-worthy reference content compounds.
- **May 2026 official AI-slop crackdown** (VP Laura Lorenzetti): generic AI posts get silently
  reach-suppressed (visible to connections, cut from wider feed; LinkedIn claims 94% detection);
  named tells include the "it's not X, it's Y" frame and engagement bait. **Automated/AI comments
  are explicitly targeted**: demoted out of "Most Relevant", hidden outside network, repeat
  offenders restricted. AI *assistance* is explicitly fine — the line is generic vs. grounded in
  your own perspective. Enforcement is behavior-triggered (velocity, sameness, cadence
  regularity, infrastructure fingerprints — cf. the HeyReach datacenter-proxy ban wave), which
  validates LEM's residential-proxy posture but raises the bar on output quality and pacing.
- **What drives impressions now** (evidence-ranked): dwell time (hook within ~140 mobile chars);
  saves (~5x a like); >5-word comments that earn author replies in the first 60–90 min (threaded
  replies ≈2.4x reach; top-1% creators reply 741% more); sends/DM-shares; topical consistency;
  cadence 2–4/wk (daily = −26%/post); document carousels 1.45x / ~6.6% ER vs ~2% for text;
  external links in body −19% to −60% (edit-in-later is the standard workaround).
- **Small-account silver lining**: 1K–5K-follower accounts average ~4.2% ER — engagement rate
  favors small accounts; personal profiles get ~65% of feed distribution vs ~5% for company pages.

## 3. Creator study — what the winners do (20 profiles)

Full dossiers live in the research (Justin Welsh, Jasmin Alić, Ruben Hassid, Charlie Hills,
Lara Acosta, Richard van der Blom, Allie K. Miller, Dan Koe, Sahil Bloom, Nicolas Cole, Zain
Kahn, Wes Kao, Katelyn Bourgoin, Ethan Mollick, Greg Isenberg, Rowan Cheung, John Rush, Matt
Gray, plus peers Nick Saraev ~28K, Isabella Bedoya ~30K, Ben van Sprundel ~20K). Recurring
patterns that separate high-engagement creators from business-goal posters:

1. **70–95% audience-value content**; promo is an *artifact* (guide, template, free report),
   never "book a call". Charlie Hills formalizes 70/20/10 (viral awareness / authority education
   with no selling / no-pressure case studies). Salesy content is penalized up to −70%.
2. **Comments on other people's posts are the growth engine** — the fastest native growers
   (Alić 50–100/day, Hassid 100–300/day, Hills 2h/day) all attribute takeoff to high-volume,
   *substantive* commenting on a curated list (Hills: 50–100 accounts split 50% peers / 30% ICP /
   20% large creators). Comments written "for the readers, not the author" — post lists 7 tips,
   you add tip #8.
3. **Hooks engineered to a character budget with real numbers** (Alić <45 chars; Hassid 2
   sentences ~55 chars; ~50% of Hills' hooks lead with a number). Specific receipts beat claims.
4. **The "build receipt" is the AI-consultant power format**: what I built → tools → what broke
   → use cases → soft CTA. Ben van Sprundel's 20-agent-team receipt did ~8% engagement on a 20K
   account — the niche's best measured result, and Christopher ships real systems weekly that
   are never posted as build logs. Liam Ottley's absence (713K on YouTube, no LinkedIn) shows
   the AI-automation practitioner niche is under-contested on LinkedIn.
5. **Golden-hour discipline is mechanics, not optional**: reply to every comment in 60–90 min
   (Alić won't post on days he can't stay); second-wave self-comment 6–8h later; 5–7 pinned
   self-comments of added insight.
6. **Fixed day-type calendars** (Alić: Mon mindset / Tue guide / Wed story / Thu polarizing;
   van der Blom: 2 docs + 2 text-image + 1 video + 1 poll weekly) build Topic Authority and
   anticipation. 2–4 high-effort posts/week beats daily volume in the 2026 regime.
7. **Personal story and spiky-but-defensible POV are scheduled features**, not accidents —
   story as evidence (numbers, dates), contrarianism aimed at a named common practice.
8. **Everyone uses AI; nobody ships raw generic output.** The pattern that survives: ideas mined
   from real conversations, drafting grounded in the author's own corpus/voice, human finishing,
   and a quality gate — Hills: *"If a stranger could have posted it, delete it."* Fully-AI posts
   measurably lose ~2.8x reach / ~5x engagement.
9. **Audience importing**: importers (Koe, Bloom, Kahn, Cheung, Mollick…) all imported *from X*,
   built pre-2025, and produce weak LinkedIn-native engagement (Matt Gray: 800K followers,
   0.42% ER). The niche-relevant winners grew LinkedIn-native via commenting + consistency —
   including 2024–2026 under the harder algorithm (Hills 0→230K, Bedoya 2K→28K). At user 1's
   size there is no external audience to import; native is the only path, and it still works.
10. **Traffic flows OUT of LinkedIn into owned email** for every creator with a business
    (newsletters 150% YoY growth, 40–60% open rates, feed-independent). LinkedIn is the top of
    funnel, never the terminal asset.

## 4. Root causes, ranked

1. **Audience-growth loops are dead** (no connections, no DMs, no reciprocity, 1 engager) —
   impressions are capped by audience size no matter how good the content gets.
2. **Comment engine is counterproductive**: generic template comments on off-topic posts is
   the exact profile LinkedIn's 2026 comment classifier demotes, and it erodes Topic Authority.
3. **Content is business-goal-first, repetitive, and receipt-free**, posted daily at −26%/post,
   with call-CTAs the algorithm penalizes.
4. **Past 429/bot-signal history** plausibly adds account-level drag (unproven mechanism), which
   argues for the suppression tripwire + pacing work rather than more volume.
5. **No measurement** of followers, comment outcomes, or suppression — the system can't see any
   of the above happening.

## 5. The plan — Milestones 13 & 14

**North star: real value to the LinkedIn community, human-grounded content, measured growth —
never volume, never slop.** Success in 90 days: follower growth measurable and positive;
≥3 author-replies to our comments/week; posts ≥2% ER on impressions; zero slop-lint failures
shipped; suppression tripwire never fired.

**Milestone 13 — Comment-First Growth Engine & Content Strategy Reset**
- G1 Target-creator engagement roster (50/30/20) + on-topic-only commenting (kill off-topic fallback)
- G2 Comment quality contract v2 + comment-side similarity/uniqueness gate
- G3 Content mix governor 70/20/10 + artifact CTAs (no "book a call")
- G4 Build-receipt & bookmarkable-resource archetypes (save-optimized)
- G5 Story bank & fact intake — human-sourced specifics, no-fabrication anchor
- G6 Cadence reset: 2–4 high-effort posts/week on a fixed day-type calendar, sane hours
- G7 Golden-hour presence hardening: reply-all + second-wave self-comment
- G8 Network activation: fix connection requests (already-connected check, name pollution,
  ICP threshold), populate outreach targets from roster/engagers, unblock DM nurture
- G9 Owned-asset CTA loop: route artifact CTAs to newsletter/lead-magnet delivery

**Milestone 14 — Anti-Slop Defense & Growth Measurement**
- D1 Deterministic slop-linter v2 across posts/comments/DMs/newsletter (LinkedIn's named tells)
- D2 Human-pacing engine: read-time delays, schedule jitter, variable daily volumes, rest days
- D3 Follower & audience telemetry (daily scrape → `follower_stats`; fix snapshot double-count)
- D4 Comment outcome tracking (author replies, likes, "Most Relevant" visibility)
- D5 Suppression tripwire: impression step-collapse → auto-pause engagement + notify
- D6 Slop-score & engagement-rate telemetry in PostHog with regression alerts

Dependencies: live Selenium selector validation for new scrape surfaces goes through the open
spikes #403/#404. The #416 humanization policy still applies: quality gates are for making
content genuinely good and genuinely ours — never for evading AI detection.

## 6. Key sources

- van der Blom Algorithm Insights 2025 (1.8M posts): scribd.com/document/984921783 · 2026 data
  via melaniegoodmanlinkedinconsultant.substack.com/p/linkedin-algorithm-2026-reach-topic-authority
- AuthoredUp ~1M-post study (upd. 2026-06-25): authoredup.com/blog/linkedin-algorithm
- 360Brew paper: arxiv.org/abs/2501.16450
- LinkedIn May 2026 AI-content reach limits: socialmediatoday.com/news/linkedin-wants-to-limit-the-reach-of-ai-generated-content/820935/ ·
  entrepreneur.com/business-news/linkedin-is-fighting-back-against-ai-slop-and-ai-comments ·
  thenextweb.com/news/linkedin-ai-slop-crackdown-generic-content
- Engagement-pod/comment-bot enforcement: socialmediatoday.com/news/linkedin-outlines-more-measures-to-combat-engagement-pods/812290/ ·
  connectsafely.ai/articles/linkedin-engagement-pods-crackdown-2026
- AI-prevalence studies: originality.ai/blog/ai-content-published-linkedin (54% of >100-word
  posts) · theregister.com 2026-07-09 Pangram study (41% of long-form)
- Creator playbooks: charliehills.substack.com/p/how-i-reached-180k-on-linkedin-with ·
  linkedin.com/posts/alicjasmin_my-last-469-linkedin-posts (Alić) · ruben.substack.com/p/1000000 ·
  justinwelsh.me/article/linkedin-strategy · viralbrain.ai/heroes/nick-saraev-7ge3obz4 ·
  Ben van Sprundel build-receipt post: linkedin.com/posts/benvansprundel_i-built-a-20-ai-agent-team
- Benchmarks: socialinsider.io/social-media-benchmarks/linkedin · contentin.io/blog/linkedin-engagement-benchmarks
