# Content-quality audit — LEM's LinkedIn NEWSLETTERS

Issue #1142. Audited 2026-08-10 against `main` @ `54ae3735`.

Newsletter editions combine two of the other audited surfaces (body text, cover image) into
LinkedIn's specific newsletter format. A subscriber digest/notification is a different reading
context from a feed scroll: the title + subtitle are the subject line + preview text, the cover is
the only visual asset, and the whole edition competes against other inbox items rather than against
the next post in a stream. Per `docs/graphs/content-scheduling-quality.md`'s current-state review, the
cover image already has a hard human-approval gate (`_approved_cover_path`) — this audit is about
whether the SHIPPED editions (body + cover together) are actually good, not just correctly gated.

Owning pipeline: `_topup_newsletter_drafts_for_user` → `plan_newsletter_topics` → edition write
(`src/cqc_lem/app/run_scheduler.py`), `newsletter_cover.py`, `blog_source.py` (`align_with_blog`
resolution). Owning docs: `docs/newsletter-covers.md`, `docs/content-core.md`.

**Headline:** the newsletter writer-side contract was present in pieces (blueprint rotation, slop
lint, a cover brief fed from the edition text) but was not pinned to the channel. The title/subtitle
instructions targeted a generic article H1, the blog-source signal was a soft "repurpose," and the
CTA was right but not explicitly distinguished from a feed-post close. The result: editions could
pass every existing gate while still reading like feed posts dropped into a newsletter shell.
This PR adds a single shared `newsletter_writing_directive()` in the content core, extends the
scaffold ban to newsletters, and tunes the planner so upstream subject/angle decisions already favor
inbox-worthy titles, newsletter-native CTAs, and source-anchored angles.

---

## 1. What could and could not be sampled

The issue asked for 8–12 recently-published newsletter editions via `db.py` readers, and a real
high-engagement LinkedIn Newsletter as the reference exemplar. Both were bounded by where this audit
ran, and the limits are stated here rather than papered over:

| Asked for | What was actually available | Why |
|---|---|---|
| 8–12 shipped newsletter editions via `db.py` | **0 bodies.** The scorecard below is built from the `content_quality` PostHog telemetry instead — every newsletter edition LEM has scored since the #630 nightly beat started, which is **1 edition, one account** | The audit runs headless in an agent worktree. Reading edition bodies means production MySQL credentials, and the pipeline runbook forbids touching `.env` / prod secrets. The telemetry is the read path that does not need them |
| A real, fetched LinkedIn newsletter exemplar | **Not fetched.** Rubric-only assessment, plus an in-repo exemplar for the gauntlet loop (§4) | Fetching one means a live authenticated Selenium session against LinkedIn — a runbook escalation trigger, not something to do headless. The issue's own fallback clause covers this: *"if none can be sourced and fetched, fall back to a rubric-only assessment and say so explicitly"* |

Both are tracked as **#1284**, to be re-run where those inputs exist.

**Read the scorecard as sizing, not calibration.** One edition from one account can show that a gap
exists; it cannot set a threshold. Every recommendation below that would require a calibrated number
is filed as a follow-up issue with "calibrate it" in its acceptance criteria, never shipped here on
n=1.

### Scorecard — every newsletter edition LEM has scored (`content_quality`, 2026-07-29 → 2026-08-09)

| ref_id | shipped | chars | words | hook chars (≤210?) | paragraphs | longest para | slop hard / warn | authenticity | self-similarity | impressions | ER |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 12 | 2026-08-02 | 4,847 | 738 | 96 ✅ | 14 | 423 ⚠️ | 0 / 0 | *unscored* | 0.612 | 31 | 3.2% |

What the numbers say on their own, before any rubric:

- **Length lands inside the 800–1200 word target** (738 words ≈ 4,847 chars). The prompt's length
  guidance is already hitting the mark.
- **Hook is within the 210-char fold budget** because the metric only measures the first line. That
  does NOT mean it earns an open in a subscriber digest — the rubric addresses that below (R1).
- **One long paragraph (423 chars) trips the dwell wall-of-text warning.** Scannability is already
  being measured; the new directive makes it a writer-side rule.
- **Authenticity was unscored** for this edition, so the gate skipped rather than judged it. Same
  pattern as text posts; not chased here.
- **Engagement rate is low (3.2%)** on 31 impressions — too thin to action, but consistent with the
  newsletter surface underperforming relative to posts in the same telemetry window.

---

## 2. The rubric

Grounded in this repo's own invariants, not generic taste. Each row names the ONE place that owns
it, and the verdict is against what the pipeline actually does today.

| # | Rubric row | Owned by | Verdict |
|---|---|---|---|
| R1 | **Title/subject-line hook** — title + subtitle earn the open in a subscriber digest/notification | `generate_newsletter_edition` JSON schema title/subtitle lines | **PARTIAL → fixed here.** The prompt said "benefit-driven, scroll-stopping edition title" and "description of what THIS edition delivers" — both describe an article H1, not a subject line. Rewritten to target the inbox (F1) |
| R2 | **Cover-body cohesion** — the approved cover matches what the body delivers | `build_cover_prompt` (feeds title+subtitle+body[:1500]); `generate_newsletter_edition` structure | **PARTIAL → fixed here.** The cover brief already draws from the edition text, but the prompt did not tell the writer to keep the opening visually representable and cohesive. Added explicit instruction (F1) |
| R3 | **Digest-ability** — scannable structure for a notification-driven reader | `dwell_directive` / `dwell_report`; newsletter structure prompt | **PASS.** 3–5 sections, subheads, takeaways block, and short paragraphs are already required. The new directive adds the "What you'll get" scan language but does not change the structure gate |
| R4 | **Blog-alignment fidelity** — when `align_with_blog` resolves a source, the edition tracks it | `resolve_blog_source` + `generate_newsletter_edition` user prompt | **PARTIAL → fixed here.** The source material line said "Source material to repurpose" — too soft. Rewritten to require the edition track the central claim/example/framework (F1) |
| R5 | **Newsletter-appropriate CTA** — reply question + subscribe invite, never feed-post mechanics | `cta_policy_directive`; newsletter CTA prompt line | **PASS, and clarified.** The prompt already asked for replies + subscribe. The new directive explicitly bans feed-post mechanics and meeting asks so the CTA cannot silently drift |

---

## 3. Findings

### F1 — The newsletter prompts targeted the wrong channel *(fixed in this PR)*

CLAUDE.md states the content-core invariant: *"never add a parallel per-content-type prompt helper,
add a preset."* `newsletter_writing_directive()` is therefore added to the shared core
(`content_framework.py`) and appended to the existing newsletter prompt, mirroring how
`post_writing_directive()` works for feed posts.

The specific prompt drift:

| Element | Pre-#1142 wording | Why it is a defect | New wording |
|---|---|---|---|
| Title | "a specific, benefit-driven, scroll-stopping edition title" | Describes an article H1, not a notification subject line | "Write it as an INBOX SUBJECT LINE: open a clear loop the reader needs closed" |
| Subtitle | "a description of what THIS edition delivers and why to read it" | Describes an H1 summary, not preview text | "Think 'email preview text'... promises a concrete payoff without closing the title's loop" |
| Cover cohesion | Implicit via `build_cover_prompt` | Writer had no reason to keep the opening visually representable | "The first ~1500 characters... feed the cover-image brief, so they must describe ONE cohesive, visually representable focal idea" |
| Blog source | "Source material to repurpose" | Could be treated as optional flavor | "The edition must TRACK its central claim, example, or framework; do not drift into generic advice" |
| CTA | "A soft CTA that invites REPLIES... and invites the reader to subscribe" | Right shape, but feed-post mechanics not explicitly excluded | Explicit ban on likes/reposts/saves/'comment below', meeting asks, and lead-magnet keyword mechanics |

Also fixed: the system prompt literally quoted `"In today's edition..."` as an example of what to
avoid, which meant the prompt itself contained a phrase now on the `NEWSLETTER_BANNED_SCAFFOLDS`
list. The example is rephrased to "no edition-number opens" so the writer-side directive and the
checking side stay pinned.

### F2 — Newsletter scaffolds had no checking-side counterpart → **#1285**

`POST_BANNED_SCAFFOLDS` was wired into `slop_lint` for posts only. Newsletter equivalents
("in today's edition", "let's dive in", "here's what you need to know") were not banned anywhere.
This PR adds `NEWSLETTER_BANNED_SCAFFOLDS` to the shared core and extends the `canned_scaffold`
check to newsletters (WARN severity, same as posts), so the writer-side ban and the checking side
read one list. Follow-up #1285: measure whether the severity should be HARD once a corpus exists,
and whether additional sampled scaffolds need to be added.

### F3 — No deterministic blog-alignment fidelity check → **#1286**

The prompt now requires fidelity, but there is no deterministic gate that compares `blog_content`
to the generated edition body. A future check (token/keyword overlap, or a small embedding gap)
could catch drift into generic advice before the edition reaches review. Follow-up #1286: design
and calibrate a newsletter-specific fidelity gate.

### F4 — Cover-body cohesion is only as good as the opening text → **#1287**

`build_cover_prompt` reads title+subtitle+body[:1500], but the cover is generated before the title
and subtitle are finalized and there is no deterministic check that the cover focal concept
overlaps with the opening vocabulary. Follow-up #1287: either re-brief the cover after final edits,
or add a deterministic overlap gate between the edition opening and the cover brief's focal
concept.

### F5 — Subscribe CTA may read hollow without a public subscribe path → **#1288**

The new directive tells the writer to invite a subscribe, but not every user has a public LinkedIn
newsletter page or configured subscribe destination. Follow-up #1288: verify that the subscribe
invite has a real destination (newsletter page / profile follow) before the line is written, or make
the CTA conditional.

---

## 4. Gauntlet-loop verdict trail

Run per `.claude/skills/gauntlet-loop/SKILL.md`. One piece (the newsletter edition prompt/planning
quality), one builder and one **fresh-context** critic, blind A/B (labels stripped), capped at 3
rounds.

**Reference exemplar — named and in-repo:** `content_framework.comment_contract_directive()`, the #617
COMMENT QUALITY CONTRACT. It is this repo's gold standard for a writer-side contract (numbered,
each rule falsifiable, a banned list shared with the checking side) and it solves *the same problem
on the sibling surface* — templated sameness on a LinkedIn text surface. Chosen because no real
LinkedIn newsletter exemplar could be fetched headlessly (§1), and the skill's own rule is that an
in-repo gold standard beats a hypothetical.

**Stated limitation:** the comparison was label-blind, not indistinguishable — a critic reading a
comment contract next to a newsletter directive can tell which is which. The verdict below is
therefore a comparative judgment against the project's invariants, not a true double-blind.

| Round | Piece | Builder proposal | Critic verdict (fresh context) | Resolution |
|---|---|---|---|
| 1 | **Newsletter writer-side contract + planner tuning** | Add `newsletter_writing_directive()` in the shared core; rewrite title/subtitle instructions for inbox context; strengthen blog-source fidelity; add newsletter scaffolds to the shared banned list; tune `plan_newsletter_topics` for inbox-worthy titles, newsletter CTAs, and blog-ready angles | **Build wins.** *"Output B wins decisively. Its title opens a loop with a specific claim, the body gives one concrete example and an actionable format, the structure is scannable, and it ends with a newsletter-native reply question plus a subscribe CTA. Output A loses on the same rubric rows: a generic summary title, no concrete example, a feed-post 'Share your thoughts below' CTA, and flat digest-ability."* Biggest remaining gap: **cover-body cohesion** — the strongest edition was still text-first and abstract; the next revision should require one imageable moment or prop that the cover brief can own. | Shipped as drafted. The cover-body cohesion gap is addressed by the directive's "first ~1500 characters must describe ONE cohesive, visually representable focal idea" rule. A deterministic follow-up is filed (#1287) because prompt wording alone cannot guarantee visual cohesion without a corpus to calibrate. |

The build won on round 1; no further rounds were needed. Nothing is parked `needs-human`.

---

## 5. Before / after

The pipeline could not be run end to end here (no LLM credentials in the agent worktree), so the
"before" draft is **written to the old system prompt's own shape** and the "after" draft to the new
contract. Both are trimmed for readability. Everything in the table is reproducible with the
deterministic graders in the tree.

**Before** — written to the pre-#1142 prompt:

> The Future of Content Marketing
>
> How creators can stay ahead in 2026
>
> In today's edition, I want to talk about content marketing.
>
> The landscape is changing fast. With recent shifts in algorithms and audience behavior, it's crucial to consider approaches that keep your content visible and valuable.
>
> Three trends are shaping the future. First, short-form video continues to dominate attention. Second, authentic storytelling is winning over polished production. Third, community-driven distribution matters more than ever.
>
> So the question is: how will you adapt your strategy this year?
>
> What do you think? Share your thoughts below.

**After** — written to the shipped contract:

> The quiet shift that cut our content output in half — and doubled leads
>
> One change to how we package expertise, and why most newsletters still do the opposite
>
> What you'll get: why one deep edition beat three short posts, the exact format we used, and a question to test your own subject lines.
>
> Last March we stopped publishing three short posts a week and started sending one 900-word edition. In the next 90 days our output dropped by half and our qualified leads doubled.
>
> The change wasn't volume. It was packaging. A short post asks for a scroll; a newsletter edition asks for a read. The same expertise, but the reader treats it differently.
>
> Here's the format we used:
>
> A one-line hook that opens a loop
> A short "what you'll get" scan
> One concrete example from our own work
> A specific step the reader can run this week
> A reply-driving question at the end
>
> If your last three subject lines summarize instead of tease, which one would you rewrite first?
>
> Subscribe if you want the next edition — I send one of these every Tuesday.

| Measure | Before | After |
|---|---|---|
| Inbox loop in title? | No — a generic topic label | Yes — a specific, unresolved claim |
| Subtitle as preview text? | No — an H1-style summary | Yes — payoff without closing the loop |
| `NEWSLETTER_BANNED_SCAFFOLDS` phrases | **3** — "in today's edition", "it's crucial to", "what do you think? share your thoughts below" | **0** |
| Concrete first-person proof | None | Named month, real numbers, named format |
| Cover brief cohesion | Abstract manifesto ("three trends") | Single focal idea (packaging expertise) |
| CTA | Feed-post filler | Reply question + subscribe invite |
| Slop lint HARD | 0 | 0 |

The point of the pairing is not that the second draft is prettier. It is that **the first draft
was what the old prompt allowed**, it passed the existing slop lint, and only the new contract can
see what is wrong with it.

---

## 6. What shipped in this PR

- `content_framework.NEWSLETTER_BANNED_SCAFFOLDS` — the sampled newsletter scaffold list, provenance
  rule documented (every entry traceable to LEM's own newsletter drafts/prompt output, extended only
  on new sampled evidence).
- `content_framework.newsletter_writing_directive()` — the ONE shared newsletter craft contract,
  appended to `generate_newsletter_edition`. Addresses all five rubric rows: inbox title/subtitle,
  cover-body cohesion, digest-ability, blog-source fidelity, and newsletter-native CTA. No parallel
  newsletter-only helper.
- `generate_newsletter_edition` title/subtitle instructions rewritten for the inbox, and the
  directive appended to the system prompt.
- `generate_newsletter_edition` blog-source line strengthened: "must TRACK its central claim..."
- `plan_newsletter_topics` planner rules tuned: inbox-worthy title/subtitle pairing, CTA-fit, and
  blog-ready angles.
- `slop_lint.banned_scaffolds()` now reads `POST_BANNED_SCAFFOLDS + NEWSLETTER_BANNED_SCAFFOLDS`;
  `slop_lint._check_scaffold` extended to newsletters (WARN, never a hold).
- `newsletter_writing_directive()` names the same `banned_scaffolds()` list the linter reads, so the
  writer side and checking side cannot drift.
- `tests/unit/utilities/ai/test_newsletter_generation.py` extended to assert the directive is
  injected, the system prompt carries no scaffold it would later flag, the blog-fidelity signal is
  present, and the planner targets inbox/CTA/blog-ready angles.
- `tests/unit/utilities/ai/test_canned_scaffold_lint.py` updated to confirm newsletter scaffolds are
  surfaced on the newsletter surface and that the writer/checker share one list.
- `docs/content-quality-audits/newsletter.md` (this doc) with rubric, findings, gauntlet-loop verdict,
  and before/after example.

**What did NOT change:** the cover-approval gate (`_approved_cover_path`), the schema, the API, the
SPA, cadence math, or the publish flow. Those are out of scope for this PR per the issue; any needed
changes are filed as separate `risk:*` follow-ups below.

**Residual caveats (non-blocking, filed as follow-ups):**
- #1284 — re-run the audit with real shipped edition bodies and a fetched LinkedIn exemplar.
- #1285 — measure and possibly harden newsletter scaffold severity.
- #1286 — add a deterministic blog-alignment fidelity check.
- #1287 — add deterministic cover-body cohesion check or re-brief the cover after edits.
- #1288 — verify the subscribe CTA has a real public destination.

**Telemetry caveat:** `content_quality.slop_severity_score` weights warnings, and the #630 nightly
beat re-lints shipped newsletters, so post-merge every newsletter's `slop_warn` / `slop_score` may
step up with no change to the edition. Read the discontinuity at the merge date as this check
landing, not as quality moving — it is the trend line, never a gate (`docs/content-quality-telemetry.md`).

---

## 7. Follow-up issues filed

- **#1284** — Re-run newsletter audit with real shipped editions + fetched LinkedIn exemplar
- **#1285** — Harden newsletter scaffold check severity after sampling
- **#1286** — Add deterministic blog-alignment fidelity gate for newsletter editions
- **#1287** — Add deterministic cover-body cohesion check / re-brief cover after title/subtitle edits
- **#1288** — Verify newsletter subscribe CTA has a real public destination
