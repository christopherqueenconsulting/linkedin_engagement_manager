# LinkedIn Re-Index Playbook

Source: a LinkedIn strategy video (urn:li:activity:7490735366031716352, 2026-08-05) from an agency operator behind top-1% LinkedIn voices. Thesis: algorithm-update posts are stale by definition — the algorithm continuously retrains on user behavior, so anything "known" is already old. Instead, do durable behaviors that trigger LinkedIn to **re-index and validate your profile**:

1. **Use LinkedIn's native "Celebrate an occasion" post types** (Project Launch / New Educational Milestone). Cited result: a dormant account went from <1K to 100K+ impressions on one milestone post.
2. **Reorder profile skills so the FIRST FIVE match your SEO keywords.**
3. **Get team/friends to endorse those five skills** — validates the positioning.
4. **Echo those skill keywords in the following week's content** — profile edits trigger re-indexing; content that matches confirms "I am who I say I am."

This playbook maps those items onto LEM (workstream 1), gives the profile update checklist for user_id=1 (workstream 2 — LEM has **no profile-write capability**, so these are manual), and the LEM config changes aligned to CQC's business and SEO goals (workstream 3).

**Positioning decision (locked with Chris):** dual-track. The keywords must serve BOTH funnels — selling AI audits (buyer-intent terms) AND landing Applied AI Engineer 1099/fractional contracts (practitioner terms recruiters search). Contracts are rated the faster win, so the availability signal in the headline is sharpened, never deleted.

---

## WS1 — Engineering roadmap (filed issues)

| Issue | What | Why it exists |
|---|---|---|
| [#1074](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/1074) | Occasion/milestone post drafts, phased. Phase 1: `occasion_milestone` archetype family + `posts.manual_publish` flag — LEM drafts, user publishes natively so the post carries the occasion entity (it has **no API entity**; `poster.py` is REST-only). Phase 2 (deferred): Selenium composer walk. | Video item 1. Phase 2 deferred: high SDUI drift risk for a ~1/month action that's cheap by hand. |
| [#1075](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/1075) **shipped** | Skills↔keyword alignment first-class: skills-change detection on re-scrape, ~14-day "re-index window" directive weaving top-5 skill keywords into generated content, SPA overlap panel with one-click "adopt skills as focus topics". Builds on the existing fallback seam `profile_niche_anchors` (`content_alignment.py:591`). | Video item 4, automated for every future profile update. |
| [#1076](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/1076) **shipped** | `POST /user/linkedin-profile/refresh` — on-demand profile re-scrape + `profile_synthesis` regen. Settings → Setup → "Refresh my profile data"; one press per user per day (Redis window, fails open), queues `update_stale_profile(force_refresh=True)` on `se_outreach`. | Makes "edit profile → LEM reacts" immediate; items 2–4 hinge on LEM seeing the new skills. |

Until #1074 ships, occasion posts are manual (see WS2). A profile edit now reaches LEM as soon as the owner presses **Refresh my profile data** (#1076); the ≤7-day `auto_refresh_profile_syntheses` beat remains the floor for anyone who does not.

### How the keyword echo runs (#1075, shipped)

Video item 4 is automated end to end — the ONE place is `utilities/profile_skills_window.py`.

1. **Skills-change detection.** Every successful profile re-scrape (`get_my_profile`, and the forced refresh inside `update_stale_profile`) compares the current top-5 skills to the `profiles.last_recorded_skills` snapshot. A changed order or set records the diff. The FIRST snapshot is not a change — a brand-new account never opens a window — and a scrape that returned NO skills is an undetectable diff, so it leaves the snapshot alone rather than wiping the baseline.
2. **14-day re-index window.** A detected change writes the top-5 keywords to Redis with a 14-day TTL (persisted, so a task retry never re-rolls the window and a restart never re-opens one).
3. **The directive.** `profile_skills_directive(user_id)` is the same shape as `focus_directive` (`content_alignment.py`) — soft subject steering, appended where the post/comment prompts already append focus steering: the four auto post archetypes that carry `user_id` (thought leadership, industry news, personal story, engagement prompt), feed comments, seed and second-wave comments, thread replies, and comment-reply follow-ups. It layers on; it never overrides the subject, and the existing gates (topic-DNA on-niche, slop lint, similarity) still decide what ships.
4. **Reconciliation panel.** Settings → Content renders top-5 profile skills against `focus_topics`, adopted ones green, with one click to merge the rest in via the existing `PUT /user/engagement-preferences` (nothing is ever removed). It reads `GET /user/linkedin-profile-skills`, which is best-effort: an unreadable profile renders no panel rather than an error.

Failure posture throughout: Redis unavailable = **no window**, never a block, logged DEBUG — this is steering, and steering that fails must not stop content from being generated.

---

## WS2 — Profile update checklist (user_id=1, manual)

### Top-5 skills (video item 2)

Reorder so positions 1–5 are (finalize exact strings against LinkedIn's skill-taxonomy autocomplete and the emplibot keyword harvest):

1. **Generative AI**
2. **Large Language Models (LLM)**
3. **AI Agents**
4. **AI Consulting**
5. **Artificial Intelligence (AI)**

Next tier (6–9): RAG, AI Strategy, AI Automation, Python. Three practitioner terms + two buyer terms = the dual-track blend.

Mechanics: Profile → Skills → reorder; pin the top skills on the profile card; additionally attach the top-5 to the CQC experience entry so they render in-context.

### Endorsements (video item 3)

The week of the reorder, ask ~10 collaborators/friends to endorse exactly those five skills. Offer reciprocal endorsements. This is manual outreach — deliberately not automated (endorsement-solicitation DMs are a spam pattern).

### Headline (dual-track rewrite)

Current: `Applied AI Engineer | I build & ship LLM · RAG · Agent systems end-to-end | 15+ yrs full-stack (Python · TypeScript · AWS) | Open to remote`

Proposed shape: `Applied AI Engineer & AI Consultant | LLM · RAG · AI Agents | AI Audits for SMBs | Open to 1099/fractional contracts`

Keeps recruiter-searchable practitioner terms, adds buyer terms (AI Consultant, AI Audits), and sharpens the availability signal for the contracts funnel.

### Role titles & career section

- CQC title: "Data Whisperer" → keyword-bearing, e.g. `Founder & Principal AI Consultant — AI Audits, LLM/RAG & AI Agent Systems`.
- Rewrite the CQC experience description around AI services — the publicly-indexed company keywords are still Magento/PHP-era.
- Resolve the duplicate CQC entries (one marked current, one ended 2023) — dupes dilute the re-index.
- Add LEM as a project/product under CQC (also the Project-Launch occasion candidate below).
- Keyword-align the Defrag Inc entry where honest.

### About section

First two lines carry the dual offer + top keywords (they're what search indexes and collapsed view shows): AI audits for SMBs + fractional applied-AI engineering (LLM, RAG, AI agents). CTA to the audit; availability note for 1099 contracts.

### Supporting surfaces

- **Featured:** audit offer link, LEM, 1–2 best-performing posts.
- **Services page:** AI Consulting, AI Development.
- **Open to Work (recruiter-only):** contract Applied AI Engineer roles — invisible to buyers, visible to recruiter search.

### Occasion posts (video item 1 — manual until #1074)

- **Project Launch:** LEM itself (real, launchable, demonstrates the skill set to both funnels).
- **New Educational Milestone:** any recent cert/coursework.
- Publish ONE during the keyword-echo week via the native composer: Start a post → More (…) → Celebrate an occasion.

### Sequence — the two-week re-index play

- **Week 1 (Monday):** apply all profile edits above + the WS3 config changes; trigger the re-scrape from Settings → Setup → "Refresh my profile data" (#1076) so the new skills reach the voice brief the same day; send the endorsement asks.
- **Week 2:** keyword-echo content week — `focus_topics` (already updated in WS3) drives posts carrying the same top-5 keywords; native occasion post mid-week.

---

## WS3 — LEM config changes (user_id=1)

Applied by hand in the Account SPA (document-only by decision — no API writes). "Verify in SPA" = current value unknown from the repo side.

| Field (surface) | Current | Proposed |
|---|---|---|
| `focus_topics` (Account → Content) | verify in SPA | 5 topics mirroring the top-5 skills: AI audits & consulting for SMBs · Generative AI implementation (LLM & RAG) · AI agents & automation · applied-AI engineering (fractional/1099) · AI strategy & governance |
| `business_goals` | verify in SPA | Both funnels, explicitly: sell AI audits → audit week → retainer; land Applied-AI-Engineer 1099/fractional contracts (priority: faster win); build authority on the keywords shared with the blog's SEO plan |
| `personal_goals` | verify in SPA | Visibility/authority framing consistent with the re-index play |
| `include_topics` / `include_keywords` (Account → Targeting; feed-comment filters) | verify in SPA | Add: generative AI, LLM, RAG, AI agents, AI audit, AI adoption, AI readiness, fractional AI, hiring AI engineers |
| `posts_per_week` / `posting_days` (Account → Content) | 3 / Mon–Fri (defaults) | **5** for the two re-index weeks, then revert to 3; keep Mon–Fri |
| `blog_url` / `sitemap_url` (Account → Setup) | verify in SPA | christopherqueenconsulting.com blog + sitemap, so `blog_summary`/`website_content` post types reinforce the same keywords |
| Newsletter settings | `align_with_blog` default ON | keep ON; phrase the newsletter topic in the keyword family |
| Lead magnet | verify in SPA | keyword e.g. `AUDIT` paying out an AI-audit checklist asset (comment-keyword → DM mechanic) |
| Connection targeting | verify in SPA | `connection_targeting_mode: suggest`; target-author/query terms toward SMB owners and teams hiring fractional AI help; `min_connection_icp_score` stays 55 |
| Caps / safety (max comments/DMs/invites, breaker, tripwire) | — | **Unchanged.** Safety controls are not tuning knobs. |

Note: `focus_topics` is the single field that changes what LEM writes about (`content_alignment.focus_directive` / `select_focus_topic` / topic-DNA gate); `include_keywords` only changes which feed posts LEM comments on. Both matter to the play, for different halves of it.
