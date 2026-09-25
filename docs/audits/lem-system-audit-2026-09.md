# LEM System Audit — 2026-09-10 → 2026-09-24

Audit date: 2026-09-24. Prod release: **v0.176.5**, verified at `/api/app-info`. Users: **1**. Method: read-only evidence gathering by three independent gatherers, then an adversarial verification pass that took a different query path, then synthesis by the lead, then a **gauntlet loop** with a blind builder/critic comparison against in-repo exemplars. The rule throughout was **nothing is believed**. Every claim cites a query, a log line, `file:line`, or a viewed artifact. Anything unverified is labelled as such.

## Headline

**LEM is running, and safely by LinkedIn's measure.** There were 0 × 429s, 0 challenges, 574/574 cookie logins, 1 static egress IP, and every configured cap held. The platform is also sound: all 56 beat entries ran on cadence, no container restarted outside a deploy, and 12 of the 49 merged PRs are verified live. The Vault note saying "LinkedIn automation retired 2026-08-20" is **wrong about the product**. LEM itself automated 102 comments, 65 DMs, 6 posts and 45 catch-up touches in the window.

**What it produces, and what it reports, cannot be trusted yet.** Five failure shapes dominate:

1. **Safety controls are undone from outside the code.** A July host cron clears the 429 breaker every day at 14:00 UTC (F1).
2. **Invented facts ship on every surface.**
   - Posts, a group post, sent DMs, and 24 of 102 comments.
   - 6 of 7 newsletters, 3 of which are approved and queued to auto-publish. One says "That's a real number from a real client."
   - The story bank contains no client work at all (F2).
3. **LEM comments twice on the same post and answers itself.** This happened 22 times in 14 days (F4). Before each post, the pre-post window hammers LinkedIn with up to 41 sessions in 30 minutes (F3).
4. **Held content ships anyway.** Paths include the carousel heal path, a vision-rejected image, and a publish with no approver recorded (F5, F21).
5. **Celery SUCCESS is read as the outcome.** There were 0 FAILURE states in 20,841 runs. Meanwhile the newsletter (dead since 08-25), company-page invites, home-feed commenting, connection requests and appreciation DMs all delivered **nothing** while reporting success (F6, F7, F10-F13).

**Reach is flat:**
- Third-party ER: 0.74% on n=6, against a verified baseline of 0.51%.
- Followers: +38.
- Author replies to our comments: 0.
- Lead-magnet deliveries: 0 ever.

The growth loops have no input.

**Filed:** 33 issues. 14 are in [Milestone 30](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/milestone/32) (3 critical, 11 high). 19 are in [Milestone 31](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/milestone/33). 7 of the 33 reopen a closed issue as a regression. See §7.3.

## 1. Goals and outcomes, aligned to LEM's purpose

LEM exists to grow the owner's LinkedIn presence and pipeline in two ways: **content generation & scheduling**, and **engagement automation**. It must never put the account or the owner's credibility at risk.

| Goal | Outcome this audit measures | Window result |
|---|---|---|
| G1 Reach and engagement growth | Third-party ER, impressions, followers, author replies | Flat: 0.74% ER (n=6), +38 followers, 0 author replies |
| G2 Content quality and alignment | Voice, story-bank fact, slop lint, 70/20/10 mix, archetype fit, **media ↔ text alignment** | Bad: fabrication on every surface; mix 35/41/24; misaligned decks and video |
| G3 Lanes deliver OUTCOMES | Attempted → landed → outcome, per lane | 5 lanes delivered 0; comments 70% unverifiable |
| G4 Account safety | 429s, challenges, pacing, session density, breaker integrity | On-platform clean, but the breaker is wiped daily and pre-post density reaches 41 per 30 min |
| G5 Platform reliability | Deploys, beat, restarts, OOM, silent success | Good infrastructure; nightly post-stats OOM |
| G6 Cost efficiency | Spend per artifact; the two cost instruments agree | $9.03 for 14 days; the two instruments disagree by about 13× |
| G7 Engineering throughput | PRs shipped, deployed and verified live | 49 merged; 12 verified live, 17 unverified; slow lane red for 24+ nights |
| G8 Observability truth | Each instrument matches ground truth | 5 blind or wrong instruments |

## 2. Scope: what was and was not sampled

| Asked for | Available | Why |
|---|---|---|
| Every action in 14 days | **Yes.** `logs` has 240 rows, every lane table was read, 15,425 log lines, and 26,137 Flower tasks | Read directly from prod `mysql_db` |
| Soft and hard failures | **Yes.** Hard: 17 ERROR, 20 tracebacks, 13 `$exception`. Soft: 14 classes counted | Log and Flower classification in §Failures |
| Every artifact reviewed for quality | Posts 7/7, newsletters 7/7, group posts 2/2, DMs (all bodies), comments (25 sampled plus regexes over all 102) | DB bodies |
| Every media artifact checked against its text | Decks 4/4 and video 1/1 (frames), covers 7/7. **Post images 100 and 102 were purged at publish**, so only their render receipts could be graded | `purge_post_assets` |
| On-platform verification (comments landed, posts live) | **Not done.** The read-only live probe was blocked by the session permission classifier | Outcomes come from the `comment_outcomes` and follow-up sweeps only |
| PostHog | HogQL events: yes. Dashboards and alerts: **no** (the key lacks `dashboard:read`/`alert:read`). The MCP returned 401 | Key scope |

## 3. Grading rubric and issue threshold

Each unit (a lane, a content surface or a subsystem) is scored 0-4 on five dimensions:
- **Outcome**, verified at the outcome.
- **Quality/alignment**.
- **Safety**.
- **Reliability**. A job that runs but fails silently scores low.
- **Observability**. Does the unit's own number match ground truth?

The scale is 4 Excellent · 3 Working well · 2 Not so well · 1 Bad · 0 Broken, dead or harmful.

**Severity:**
- **S0**: account, data or security risk, or a harmful artifact published.
- **S1**: a lane or feature delivers nothing, or reports false success.
- **S2**: a degraded, measurable gap.
- **S3**: minor.

**Threshold:**
- S0/S1 are **always filed**.
- S2 is filed with ≥ 2 corroborating sources, or when its unit scores ≤ 2 on the dimension the finding is about.
- S3 is filed only when the fix is trivial.
- Everything else goes on the **watch list**.

**Priority:** S0 → `priority:critical`, S1 → `priority:high`, S2 → `priority:medium`, S3 → `priority:low`.

**Milestones:**
- **Milestone 30: Audit 2026-09 — Safety & Broken Lanes** (S0/S1).
- **Milestone 31: Audit 2026-09 — Quality & Growth** (S2/S3).

`agent:ready` is set only when every acceptance box has a concrete verifier and no product decision or documented invariant is being decided. Otherwise the issue gets `needs-human` / `risk:product-decision`.

## 4. Actions ledger (logs table, result=success unless marked)

| day | comment | dm ok/fail | engaged ok/fail | followup | post |
|---|---|---|---|---|---|
| 09-10 | 4 | 5/0 | 1/0 | 0 | 0 |
| 09-11 | 4 | 5/1 | 1/17 | 3 | 2 |
| 09-12 | 8 | 3/0 | 0/0 | 5 | 0 |
| 09-13 | 6 | 5/0 | 0/0 | 3 | 0 |
| 09-14 | 10 | 6/0 | 2/0 | 0 | 1 |
| 09-15 | 2 | 5/1 | 2/8 | 0 | 1 |
| 09-16 | 8 | 7/2 | 0/0 | 0 | 0 |
| 09-17 | 11 | 5/0 | 0/9 | 0 | 1 |
| 09-18 | 8 | 4/0 | 0/0 | 1 | 0 |
| 09-19 | 9 | 4/0 | 0/0 | 1 | 0 |
| 09-20 | 8 | 2/0 | 0/0 | 0 | 0 |
| 09-21 | 7 | 7/0 | 0/0 | 0 | 0 |
| 09-22 | 9 | 5/0 | 4/6 | 0 | 1 |
| 09-23 | 8 | 2/0 | 0/0 | 0 | 0 |
| **total** | **102** (90 third-party + 12 own-post seeds) | **65/4** | **10/40** | **13** | **6** |

Other lanes in the window:
- Catch-up: 45 sent, 38 skipped.
- Group posts: 1 published, 1 failed.
- Newsletter: 0 of 2 published.
- Company-page invites: 0.
- Connection requests: 0 ledger rows. One invite came through the profile-viewer path.
- Stale-invite withdrawal: 1.
- Appreciation: 0.
- Lead magnet: 0.
- Reply rows: 0.

Cross-checks: `metrics.jsonl` agrees with the DB once its 23:30 UTC cut is allowed for. The 90 third-party comments equal the 90 `commented_posts` rows. There are 3 ledger mismatches (L-1..3, in F13/F15).

**Spend:** $9.03 on the cost ledger, of which comments were $5.04 and one veo3.1 video regeneration was $2.40.



---

## 5. Scorecard, what works and what doesn't, failures, and findings

Window: 2026-09-10 00:00 UTC → 2026-09-24 ~01:35 UTC. Deployed release v0.176.5.
Every number below comes from one of four input files. Each citation names the file and, where the file has one, its local source id:

| Tag | File | Local ids it carries |
|---|---|---|
| **A** | `p1-A.md`: actions ledger, lanes, account safety, failures | DB-1..6, FL-1..3, RD-1, MJ-1, LOG, CODE-1, L-1..3, A-1..15 |
| **B** | `p1-B.md`: artifacts, text and media quality, performance | q1-3.py, d1-3.raw, a1-a10.py, pairs/sample/gram/lint/days/nlscan.py, `media/`, B-1..15 |
| **C** | `p1-C.md`: platform, cost, engineering, observability | C-1..12, *Platform*, *Failures*, *Cost*, *Observability truth*, *Security* |
| **V** | `p2-verify.md`: an independent re-derivation of each claim | the verdict row for each claim |

**V overrides the gatherers.** When V corrects a gatherer, this section uses V's figure and says so. There are four such cases:
- **B-5** (the "[link]" follow-ups) is a false-success ledger row. The placeholder guard refused all three sends, so no broken DM reached anyone. Severity drops S1 → S2.
- **B-4** (the meeting ask in a DM) is confirmed on DM 37, which was sent.
- **A-7** counts **15** home-feed scans, not 14.
- The comment-outcomes denominator is **14 checked**, not 13.

`scratchpad/v_summary.txt` is an earlier verifier pass. It marked A-1, A-2, A-7 and A-9 PARTIAL or UNVERIFIABLE because its method could not see the hash keys. V then re-derived all four by a different path (log-line pairing, "Engagement scan" timing, the Flower result text) and confirmed them. V's verdict stands. The evidence column shows which path did the confirming.

Scores use the rubric's 0-4 scale on five dimensions:
- **O**: Outcome, verified at the outcome.
- **Q**: Quality or alignment of the artifact.
- **S**: Safety.
- **R**: Reliability. A job that runs but fails silently scores low here.
- **Ob**: Observability. Does the unit's own reported number match ground truth?

`n/a` means the unit produces no artifact, or has no schedule of its own.

**How the issue threshold is read.** The rubric has two rules that can clash:
- "File if a unit dimension scores ≤ 2."
- "A single-source S2/S3 goes on the watch list only."

This section treats the evidence requirement as the gate that decides:
- **S0/S1**: always FILE.
- **S2**: FILE when ≥ 2 corroborating sources back it. A corroborating source is either a distinct evidence source id (S-DB, S-LOG, S-PH, S-GH, S-REDIS, S-ASSET, code, Flower) or an independent confirmation in V. With only one source it is WATCH, even when its unit scores ≤ 2.
- **S3**: FILE only when the fix is trivial.

A unit that scores ≤ 2 with no fileable finding behind it is named in the WATCH list, so nothing low-scoring goes unreported.

---

### 5.1 Scorecard

#### 1a. Engagement lanes

| Unit | O | Q | S | R | Ob | Evidence pointer |
|---|---|---|---|---|---|---|
| Feed commenting: **home feed** | **0** | n/a | 3 | 2 | 1 | V A-7: 15 home scans (outside 17-22h UTC), 0 comments. "passed filters 0" on every one. fallback=False 69/69 (A §4b) |
| Feed commenting: **group feeds** | 1 | 1 | **1** | 2 | 1 | 90 third-party comments (A §1b). 22 of 67 "Commented on" lines are the second comment on the same post (V A-1). 6 comment-removed vs 14 checked, and all 14 have 0 likes and 0 replies (V A-2). Out of time in 11/14 runs (A §4a) |
| Replies / seed comment | 2 | 2 | 3 | 3 | 2 | 6/6 posts seeded plus 6 second waves, within `SELF_COMMENT_MAX_PER_POST=2` (A §1a). Own-post comments grade Q2 (B §2: 1230, 1279, 1298, 1417). Mention walk blind 9× (A-12) |
| Golden-hour presence | 2 | n/a | 3 | 3 | 2 | Dispatch 14/14, with sweeps at 22/42/62 min (A §2). `golden_hour_report` counts our own seed as "comment found" (C *Observability truth*) |
| Human pacing | 3 | n/a | 2 | 4 | 2 | No lane exceeded a cap: comments 11/20, DMs 7/20, catch-up 7/10 per day (A §2). It bounds daily counts but not spacing within a run: 7 DMs in 4m50s (A-10). "budget spent (cap 20)" printed 124× when the pacing draw was the real limit (A-13) |
| Pre-post window / profile-viewer engagement | 2 | 3 | **0** | 1 | 2 | Up to 41 sessions in 30 min vs a baseline of 9-15, and one viewer revisited 5-9× in about 15 min (A-3; V: 19 launches in 30 min on 09-17). 40 engaged/failure rows (A §1a). Cold DMs grade Q3 (B §2) |
| DM auto-nurture | 1 | 1 | 1 | 1 | 2 | 5 stale drafts sent in 2.5 min at 00:01 UTC after a 7h20m requeue loop (A-4, V). DM 37 carried a meeting ask and a fabricated claim (V B-4). "Hi Giri" draft to Saikumar (V A-4) |
| DMs + follow-ups | 2 | 1 | 2 | 2 | 1 | 10 follow-up DMs landed. 3 rows read `success` for DMs the guard refused (A L-1; V B-5). "Sent 5" reported when 2 landed (A L-1). 5 recipients were nudged on consecutive days (B §2) |
| Catch-up touches | 3 | 2 | 2 | 4 | 3 | 45 sent = 45 attempts, 38 birthdays skipped (A §1b). Templated but acceptable, Q2 A3 (B §2). 7 DMs in 4m50s on 09-21 (A-10) |
| Appreciation sources | **0** | n/a | 4 | 2 | **0** | 0 touches. "Found 0 recommendation(s)" 38/38 (V A-9). Returned "Appreciation DMs Sent" 44/44 (A L-3) |
| Reciprocity | 0 | n/a | 4 | n/a | 2 | `post_engagers` holds 1 row ever and 0 in the window (A §1b). By the lane's own invariant this is an **audience fact**, so it rolls into F27 |
| Owned-asset CTA loop | 0 | n/a | 3 | 2 | 2 | `lead_magnet_sent` has 0 rows ever, despite AUDIT CTAs on posts 96 and 102 (B §4). There are no third-party commenters to trigger it (see F27) |
| Comment outcomes sweep | 2 | n/a | 4 | 3 | 1 | Runs 14/14, but only URN-keyed comments are eligible. 63/90 (70%) are hash-keyed and never measured (V A-2) |
| Suppression tripwire | 2 | n/a | 3 | 4 | 2 | "0 newly tripped" 14/14 (A §3). Per C, the readings sat at `watch` for 9 days during a 75-91% impression drop and never tripped. **Unverified**: V did not check it, and A did not measure the readings |
| Groups sync + reconcile | 3 | n/a | 4 | 4 | 2 | "Synced 50 group(s)" 2/2. 10 groups are disabled but still post_enabled (A §2) |
| Roster targets | 1 | n/a | 3 | 2 | 2 | About 1 roster comment a day. "No commentable on-topic post found for roster target" 144×. Follow-control drift 3× on 09-13 (A §2) |
| Roster connect escalation | 0 | n/a | 4 | n/a | 2 | 0 transitions. `connect_status` is unknown on 82/82 targets, so nothing enters the ladder (A §1b) |
| Stale-invite withdrawal | 2 | n/a | 4 | 3 | 3 | 1 withdrawal and 11 none_stale. 2 budget_reached with withdrawn_today=0 (A §2) |
| Company-page invites | **0** | n/a | 4 | 1 | **0** | 0 invited in 14 days. Lane sessions read "Credits 0/0" 15× while Live-Validation sessions read 50/50 4× (V A-5) |
| Connection requests | **0** | n/a | 4 | 3 | 1 | Last `connection_requests` row 09-07 14:03 (V A-8). "No Connection Requests to Send" 4052/4052 (A §2). The one confirmed invite has no ledger row (A L-2) |

#### 1b. Content surfaces

| Unit | O | Q | S | R | Ob | Evidence pointer |
|---|---|---|---|---|---|---|
| Posts (text / carousel / video) | 2 | 2 | **1** | 2 | 2 | 6 posted, 3P ER 0.74% on n=6 (B §4). Per-post Q/A: 2/2, 1/1, 2/2, 3/3, 0/1, 3/3 (B §2). 105 shipped with a HARD slop hit and 108 was auto-approved by the heal path (V B-3). 2 posts per week vs posts_per_week=3 (B-10) |
| Newsletters | **0** | **0** | 1 | **0** | 1 | 0 published since 08-25, editions 9-13 `failed` (V C-2). A0 on 6/7 editions for fabricated client stories (B §2). Editions 14-16 are approved and auto_publish=1, next due 09-29 13:00 (V) |
| Group posts | 2 | 1 | 2 | 2 | 3 | 1/2 published (09-22). 09-15 failed with "Group share box not found" (A §2). The published draft 6 carries an invented "40% drop in per-call cost" (B §2) |
| Comments (the artifact) | 1 | **1** | 1 | 3 | 2 | Sample of 25: mean Q 1.28, A 1.52. 24/102 carry an invented first-person number. 13/102 use a banned phrase (B §2). 110 quality-contract skips show the guard running (B §5) |
| DMs (the artifact) | 2 | 1 | 1 | 2 | 1 | Nurture ranges Q0-Q3 and DM 37 is Q0/A0. The contraction pass corrupts grammar in DMs 1299 and 1303 (V B-7). Slop lint: 0 HARD on DMs, and it does not see fabrication (B §2) |
| Media (images, video, decks, covers) | 2 | 2 | 2 | 3 | 1 | Post 100's image shipped with gate_verdict `rejected` (V B-8). Deck 105 scores 0/4 alignment, and deck 110 promises 5 gates but shows 4 (B §3). Covers ed14/15/16/18 score 4/4. 5/6 published items had their media purged (B §3) |

#### 1c. Platform

| Unit | O | Q | S | R | Ob | Evidence pointer |
|---|---|---|---|---|---|---|
| Deploys + CI | 3 | 2 | 3 | 2 | 2 | 49 PRs shipped (v0.173.13..v0.176.5). 0 rollbacks and 1 SSH-timeout redeploy failure. 11 named PRs verified live vs 17 unverified. Nightly slow lane red for 24+ nights (C-9). Host crons run 31 commits behind (C-12) |
| Celery + beat + workers | 3 | n/a | 3 | 2 | **0** | 56/56 beat entries ran on cadence, RestartCount 0, Grid 8 slots = the concurrency sum (C *Platform*). 0 FAILURE/RETRY across 20,841 window runs (C-3). Chrome OOM on 3 nights (C-4) |
| Post-stats scrape (nightly) | 2 | n/a | 4 | 1 | 1 | Rows per night fell from 14 to 7, 4, 5 after "Browser tab crashed" on 09-21/22/23. A once-daily warning never escalates (C-4) |
| Observability (PostHog, logs, error→issue) | 2 | n/a | 3 | 2 | 1 | 13 `$exception` events in 7 groups. `engagement_rate` counts own comments (C-5). The margin series is null on 48/63 rows (C-7). The error→issue cron skips regressions (C-8). The personal key lacks alert/dashboard read (C *Observability truth*) |
| Cost | 3 | 2 | 4 | 3 | 1 | $9.03 over 110 ledger rows in the window. About 20 LLM calls per landed comment. 104/110 rows have NULL task_name (C *Cost*). Ledger and `$ai_generation` disagree by about 13× (C-6) |
| Account safety | **4** | n/a | 2 | 4 | 3 | 0 × 429, 0 auth walls, 0 challenges. Cookie login 574/574. One egress IP 581/581 (A §3). But the breaker is wiped daily at 14:00 (V C-1), and the pre-post session bursts are real (A-3) |
| Secrets + auth surface | **4** | n/a | 4 | 4 | 3 | ENCRYPTION_REQUIRED=true. No /admin in openapi. Admin returns 401 unauthenticated. 4 passkey logins, 0 failures (C *Security*; A §1b) |

---

### 5.2 Working well, not so well, bad

#### Working well (a dimension at ≥ 3, with evidence)

- **Account safety held for 14 days.** There were 0 × 429s, auth walls, challenges or checkpoints, and no password or PIN logins. Cookie login succeeded 574/574. Every session used the one static residential egress, 581/581, with 0 proxy errors. Redis holds no cooldown or pause key. All 14 automation pauses were deploys (A §3).
- **The beat schedule and workers are healthy.** 56/56 beat entries ran on cadence. RestartCount is 0 everywhere. The 8 Grid slots equal the lane concurrency sum. /health/deep is healthy, disk sits at 17%, and 22 of 32 GB memory is free (C *Platform*).
- **Deploys are safe.** 0 rollbacks across v0.173.13..v0.176.5. The only incident was one SSH-timeout redeploy failure. **11 PRs were verified live**: #2014, #2019, #2024, #2045, #2051, #2053, #2054, #2058, #2060, #2062, #2075 (C *Engineering*).
- **Caps held.** No lane passed a configured cap: at most 11 of 20 comments, 7 of 20 DMs and 7 of 10 catch-ups per day. Own-post comments stayed at exactly 2 per post, the `SELF_COMMENT_MAX_PER_POST` limit (A §1a, §2).
- **The catch-up lane is reliable and honest.** 45 sends match 45 attempts 1:1 by day. 38 birthdays were skipped. Its metrics.jsonl ledger agrees with the DB (A §1b-c).
- **The comment quality contract fires.** It skipped 110 drafts, 89 of them for invented first-person specifics, and hit one similarity skip at 0.85 against a 0.82 maximum (B §5). Slop lint agrees with the CQS rows: slop_hard is 0 on all 105 comment rows (B §2). The guard works. The weakness is what it does not see (F2).
- **Groups sync, golden-hour dispatch and seed comments ran every time.** Sync 2/2 ("Synced 50"), dispatch 14/14, seeds 6/6 (A §2).
- **Secrets and the auth surface are clean.** ENCRYPTION_REQUIRED=true, admin routes are hidden and return 401, and no login failed (C *Security*).
- **Most newsletter covers are on-message.** ed14, ed15, ed16 and ed18 each score 4/4 topic and 4/4 support, and no AI render carries text or logos (B §3).
- **Some instruments are accurate.** feed_scan `commented` (90), the metrics.jsonl daily counts and comment_outcomes all match the DB (A §1c; C *Observability truth*).
- **Error volume is down.** 13 `$exception` events in the window, against 233 on 09-02 alone (C *Failures*).

#### Not so well (score 2)

- **Replies, seed and golden hour** run on time. But with 0 third-party commenters, every "1 comment found" is our own seed, and the mention walk went blind 9× (A-12).
- **Profile-viewer cold DMs** read well (Q3). The lane around them loops (see Bad).
- **DMs + follow-ups.** 10 follow-ups landed, but the ledger overstates what was sent (F15).
- **Group posts.** 1 of 2 published, and the one published carried an invented stat.
- **Posts.** 6 shipped on schedule mechanics, with Q/A averaging about 2. Two slots per week are missing, and the mix runs 35/41/24 against a 70/20/10 target (B-10).
- **Media.** The covers are strong. Decks 105 and 110 and video 101 are misaligned with their posts.
- **Comment-outcomes sweep and stale-invite withdrawal.** Both run clean, but the outcomes sweep covers only 30% of comments.
- **Deploys/CI reliability.** The nightly lane is red. Releases are held and redeployed by hand.
- **Human pacing.** It enforces the daily count but not the spacing between sends.

#### Bad (score 0-1)

- **Newsletters.** O0 Q0 R0: nothing published since 08-25, and the queued editions contain fabricated client stories.
- **Home-feed commenting.** O0: 0 comments in 15 scans.
- **Company-page invites.** O0 Ob0: 0 in 14 days, blocked by a 0/0 credit reading.
- **Connection requests.** O0: idle since 09-07.
- **Appreciation.** O0 Ob0: sent nothing while reporting "Sent" 44/44 times.
- **Roster connect escalation (O0) and roster targets (O1).** Starved of input.
- **Pre-post window.** S0: 41 sessions in 30 min.
- **Group-feed commenting.** S1 Q1 Ob1: double comments, 30% of measurable comments removed, 70% never measured.
- **DM auto-nurture.** O1 S1: a stale burst that included a meeting ask.
- **Posts S1.** A gate-held post was published through the heal path.
- **Comments and DMs Q1.** Invented first-person numbers.
- **Celery Ob0.** Zero FAILURE states in 20,841 runs.
- **Post-stats R1.** OOM on 3 nights.
- **Cost Ob1.** The cost instruments disagree by about 13×.
- **Observability Ob1.**
- **Reciprocity and CTA loop.** O0, but this is an audience fact per the lane invariant, and it rolls into F27.

---

### 5.3 Failure audit

**HARD** means the failure surfaced at ERROR or CRITICAL, as a traceback or exception, or as a CI or deploy failure. **SOFT** means it was silent, a false success, or a degraded result.

Window totals: 0 CRITICAL, 17 ERROR, 419 WARNING, 20 traceback blocks.

Celery recorded 0 FAILURE and 0 RETRY:
- 20,841 window runs (C-3).
- 26,137 tasks retained in Flower (A FL-1).

The instrument does work: it recorded 18 FAILUREs between 07-04 and 09-05 (C-3). PostHog shows 13 `$exception` events in 7 groups. There was 1 deploy SSH failure and 0 rollbacks (C *Failures*).

#### 3a. HARD failures

| Class | n | Per-run rate | Window | Surfaced? | Owner-visible? | Source |
|---|---|---|---|---|---|---|
| Newsletter publish flow did not complete (article-editor selector miss) | 2 ERROR | 2/2 runs (100%) | 09-15, 09-22 | ERROR, but `log_error` has no `exc=`, so no `$exception` and no issue. Celery SUCCESS | **no** | A §4a; V C-2 (newsletter.py:181,241) |
| Orphaned scheduled DM re-queue (+~222 WARN) | 6 ERROR | 1 episode, 7h20m | 09-15 16:41 → 09-16 00:02 | escalated. Celery SUCCESS | partly (#2078 exists) | A §4a; C *Failures* |
| Mention-card walk matched nothing (+7 WARN) | 2 ERROR | 3 post days | 09-14 → 09-23 | escalated, but the error→issue cron skips it because #1985 is closed | **no** | A §4a; C-8 |
| YouTube OAuth invalid_grant | 2 ERROR | 2/2 weekly checks | 09-16, 09-23 | yes | yes (#1094 open since 08-07) | A §4a; C-3 |
| Group commenting ran out of time (+10 WARN) | 1 ERROR | 11/14 runs (79%) | 09-10 → 09-23 | escalated once | partly | A §4a |
| Roster follow-control walk matched nothing (+2 WARN) | 1 ERROR | 1 day | 09-13 | escalated | partly | A §4a |
| Unsendable DM refused, unfilled_placeholder (+2 WARN) | 1 ERROR | 3 refusals | 09-12 12:32-12:36 | escalated, but the ledger row reads `success` (see SOFT) | partly | A §4a; V B-5 |
| Reaction did not register (+2 WARN) | 1 ERROR | 1 day | 09-18 | escalated | partly | A §4a |
| Carousel could not parse into a slide model | 1 ERROR | 1 | 09-18 01:30 | yes | yes | A §4a; C *Failures* |
| Browser tab crashed (groups, post-stats) | A: 5 caught at WARN. C: 7 tracebacks | post-stats 3/14 nights; groups 2 runs | 09-13 → 09-23 | **silent** (WARN, never escalates) | **no** | A §4a; C-4. The two counts differ and are not reconciled |
| ElementClickIntercepted, inline composer | A: 3. C: 6 | — | 09-16 → 09-21 | silent | no | A §4a; C *Failures*. The two counts differ |
| Nightly Slow Tests (`test_baseline_matches_the_real_count`, 2617 vs 0) | 24+ nights | 100% of nights since ≥ 08-31 | whole window | CI red on a non-required lane | only if the owner looks | C-9 |
| Deploy SSH-timeout redeploy | 1 | 1 of the window's deploys | — | yes | yes | C-11 |

#### 3b. SOFT failures (silent, false success, degraded)

| Class | n | Per-lane rate | Window | Surfaced? | Owner-visible? | Source |
|---|---|---|---|---|---|---|
| Same-post double comment (second comment under a new feedpost:// hash) | 22 extra comments | 22/67 "Commented on" lines (33%) | 09-10 → 09-23 | **silent**: both logged success | no | V A-1; B-1 |
| "Landed" comment reads comment-removed on outcome sweep | 6 | 6 of 20 outcome reads (30%) | 09-12 → 09-22 | status column only | no | V A-2 |
| Hash-keyed comment: no permalink, never outcome-checked | 63 | 63/90 third-party (70%) | whole window | silent | no | V A-2 |
| Comment never seen on a follow-up revisit ("0 ours") | 9 URLs | 9/29 revisited | 09-13 → 09-23 | silent | no | A §4b |
| Visible comments with any engagement | 0 likes, 0 replies | 0/14 checked | whole window | — | no | V A-2 |
| Home-feed scan passes 0 posts through the filters | 15 scans | 15/15 (100%) | whole window | Redis funnel only | UI funnel, unverified | V A-7 |
| Company-page credits read 0/0 and treated as credits_exhausted | 15 reads | every lane run invited 0 | 09-10 → 09-23 | INFO only | no | V A-5 |
| Appreciation returns "Appreciation DMs Sent" with 0 sent | 44 | 44/44 | whole window | Celery result text only | no | A L-3; V A-9 |
| Follow-up ledger `success` for a DM the guard refused | 3 | 3/13 followup rows | 09-12 | 1 escalated ERROR | partly | A L-1; V B-5 |
| `process_user_followups` returned "Sent 5" when 2 landed | 1 run | — | 09-12 12:30 | Celery result only | no | A L-1 |
| Stale nurture DMs sent 3-9 days late in a 2.5-min burst | 5 | 5/5 nurture sends | 09-16 00:01-00:03 | silent | no | A-4; V A-4 |
| Pre-post loop: engaged/failure revisits to the same viewer | 40 rows | 5 post days | 09-11 → 09-22 | INFO | no | A-3; V A-3 |
| Pre-post re-runs that end "pass complete - 0 comment(s)" | 32 | 5 post days | 09-11 → 09-22 | INFO | no | A §4b |
| Misleading "Daily comment budget spent (cap 20)" | 124 lines | 3 days (2-6 comments each) | 09-11 → 09-15 | INFO, wrong text | no | A-13 (feed.py 2727-2733) |
| Held post published (gate bypass or unrecorded approver) | 2 posts (105, 108) | 2/2 held posts shipped | 09-17, 09-18 | silent. `gate_reason` left stale | no | V B-3; B §5 |
| Vision-rejected image published | 1 (post 100) | 1/2 post images | 09-11 | silent | no | V B-8 |
| Newsletter Celery SUCCESS with 0 published | 2 | 2/2 | 09-15, 09-22 | ERROR, see HARD | no | V C-2 |
| Post-stats rows lost to OOM | 14 → 7, 4, 5 per night | 3/14 nights | 09-21 → 09-23 | WARN once per day, never escalates | no | C-4 |
| Suppression tripwire at `watch` 9 days with a 75-91% impression drop | 9 days | — | — | never trips | no | C *Failures*. **Unverified** |
| Quality-contract skip (guard working; LLM spent, no comment) | 110 | about 8/day | 09-10 → 09-23 | WARNING | yes | A §4b; B §5 |
| Unknown or unreadable reads (Paul thread 16, mention walk 9, roster 3, other 5) | 33 | — | — | WARNING | partly | A §4b |
| Cost instrument disagreement | about 13× | — | whole window | silent | no | C-6 |

---

### 5.4 Findings

Ranked most severe first. Merges across gatherers:
- A-1 = B-1
- A-6 = B-6 = C-2
- C-5 ≈ B-13 (engagement_rate)
- B-2 + C-10 (fabrication)
- A-4 + A-10 + B-14 (nurture and send bursts)
- A-9 + L-1 + L-3 + C-3 split into a platform instrument finding (F7) and lane outcomes (F13, F15)
- A-12 kept separate from C-8

Milestone **M30** = *Milestone 30: Audit 2026-09 — Safety & Broken Lanes*. **M31** = *Milestone 31: Audit 2026-09 — Quality & Growth*.

"Dedupe" names the existing issue to check before filing. A duplicate gets a comment with the new evidence, not a new issue.

---

**F1. A host cron wipes the 429 breaker every day.** **S0** · G4, G8
- **Statement:** a July diagnostic cron runs `clear_rate_limit()` unconditionally at 14:00 UTC, before its own API call. The one hard account-safety gate (the breaker *blocks* a navigation, where pacing only *delays* it) is erased daily. The probe never self-disables: its API call returns 401, and it logs "inconclusive... will retry tomorrow". No 429 happened in the window (A §3), so the risk is latent: a real trip after 14:00 would be cleared at the next run.
- **Evidence:**
  - `crontab -l` → `0 14 * * * /home/lem/recovery-probe/probe.sh`.
  - `probe.sh:25-28` calls clear_rate_limit() before the API call.
  - `grep -c "probe start" probe.log` → 63.
  - The last run on 09-23 got API 401 → "inconclusive" (C-1; V).
- **Verification:** CONFIRMED (V, lead).
- **Decision:** **FILE**, `priority:critical` + `risk:*` + `needs-human` (the owner decides whether the probe is retired or made conditional) · **M30**.

**F2. Invented first-person facts ship on every surface.** **S0** · G2
- **Statement:** the story bank holds 7 entries. None mentions a client, a cost-savings percentage or an e-commerce deployment (B §1). Published and sent artifacts still claim results that do not exist:
  - Posts 100, 101 and 102, e.g. 101: "shaving a double-digit percentage off the monthly bill".
  - Published group post 6: "40% drop in per-call cost".
  - Sent DMs 1300 and 1303 ("DoD LLM rollout that cut false positives 30%").
  - 24/102 comments carry an invented first-person number.
  - 6/7 newsletters. NL14 says "That's a real number from a real client". Editions 14, 15 and 16 are **approved** and `auto_publish_newsletters=1`, with the next due **2026-09-29 13:00** (V).

  The quality contract catches some of this (89 skips) but not all. The post repair pass ran on 101 and it still shipped (B §5).
- **Evidence:**
  - `SELECT * FROM story_bank` → 7 rows, 0 mention "client".
  - B `scratchpad/nlscan.py`, `a2.py`, `a4.py`, `a5.py`, `sample.py` over `d1.raw`/`d2.raw`.
  - logs 1303.
  - C-10 quotes ("We wrapped an LLM around our inventory-allocation engine…").
- **Verification:** CONFIRMED (V, lead: story_bank 0 "client"; the NL14 text; logs 1303).
- **Decision:** **FILE**, `priority:critical` + `risk:product-decision` + `needs-human`. The owner must choose either to widen the bank or to hard-block unbanked specifics. Editions 14-16 need owner review before 09-29 · **M30**.

**F3. The pre-post engagement loop re-dispatches every ~85 s.** **S0** · G4, G3
- **Statement:** on post days, `automate_commenting` and the profile-viewer engagement re-dispatch every ~85 s for the whole pre-post window. That drives up to **41 LinkedIn sessions in 30 min**, against a baseline of 9-15. The same viewer profile was revisited 5-9× in about 15 min: Deepak Yadav 9× on 09-17, and Yavuz Yildiz and Raimund Laqua 8× each on 09-11. Each revisit writes an engaged/failure row, 40 in total. #2082 was meant to address the engaged failures, but they continue on 09-22 (6 of 10).
- **Evidence:**
  - A `dens.py` (peaks: 41 at 09-11 16:20, 38 at 09-15 12:02).
  - DB-2 rows 1209-1229, 1282-1293, 1322-1332, 1395-1406.
  - Redis `engagement:prepost:*`.
  - V: 19 task launches in 30 min on 09-17, 60-90 s apart.
- **Verification:** CONFIRMED (V, verifier).
- **Decision:** **FILE**, `priority:critical` + `risk:*` · **M30** · dedupe: #2082 (claimed fix, unverified live).

**F4. LEM comments twice on the same third-party post, and the second answers its own first.** **S0** · G3, G2, G4
- **Statement:** 22 of 67 group-feed "Commented on X's post" lines are a "(2/2)" comment on the same author at the same post age, 74-98 s after the "(1/2)", under a different `feedpost://` hash. The second comment grounds on **our own** first comment. For example, 1422 says "Try adding a nightly benchmark…", then 1423 says "…nightly benchmarks were spiking…". Both are logged success. Kirill Pokidov received 10 comments in 12 days (B-1). The hypothesized mechanism is still unverified: the walk re-reads the card's text (now our comment) as a new post, so `_feed_post_key` (feed.py:512-518) mints a new key.
- **Evidence:**
  - `grep -h "Commented on .*'s post (score" /opt/lem/logs/cqc_lem_2026_09_1*.log /opt/lem/logs/cqc_lem_2026_09_2*.log | python3 scratchpad/pairs.py`.
  - `cqc_lem_2026_09_12.log:583-586` shows two hashes 89 s apart on Mirazul Islam.
  - `scratchpad/lead_pairs.txt`.
- **Verification:** CONFIRMED in logs (V, lead). That both comments sit on the same post on-platform has **not** been spot-checked, because hash keys carry no URL.
- **Decision:** **FILE**, `priority:critical` + `risk:*` · **M30**. Acceptance must include an on-platform check of one named pair.

**F5. A held post can be published without a gate re-check.** **S1** · G2
- **Statement:** `regenerate_post_carousel_task` sets APPROVED on any non-POSTED post once real slides exist. It re-runs no authenticity, slop or asset check, and leaves `posts.gate_reason` stale. Post 108 went held → "healed to approved" (09-18 03:16) → published, with no human involved. Post 105 was held PENDING on 09-13 and published 09-17 with HARD `banned_lexicon` and authenticity 55 (the minimum is 60). No approval event exists anywhere: the approvals table has 0 rows for window posts, so the approver is unrecordable.
- **Evidence:**
  - `run_content_plan.py:2023-2027`.
  - `grep "healed" /opt/lem/logs/cqc_lem_2026_09_18.log`.
  - `SELECT id,status,authenticity_score,gate_reason FROM posts WHERE id IN (105,108)`.
  - `content_quality_scores` ref_id=105 shows slop_hard=1.
- **Verification:** CONFIRMED (V, verifier, code). How 105 got approved is unmeasured.
- **Decision:** **FILE**, `priority:high` · **M30**. It also covers recording the approval actor.

**F6. Newsletter publishing has been dead since 08-25.** **S1** · G3, G5, G8
- **Statement:** editions 9-13 are `failed` and all `published_at` values are NULL. In the window, 0/2 due editions published (09-15, 09-22). The article-editor title, body and next selectors all miss, and both sessions ran `images=blocked`, the same shape as the image-block defect. Celery reports SUCCESS. `log_error` is called without `exc=`, so no `$exception` or issue is filed. Covers and LLM spend are still paid ($0.33 newsletter LLM). The `newsletter_page` SDUI sweep has been blind for 3+ sweeps.
- **Evidence:**
  - `grep -n "Article editor\|publish flow" /opt/lem/logs/cqc_lem_2026_09_22.log`.
  - `newsletter.py:181,241`.
  - `SELECT id,status,published_at FROM newsletter_editions WHERE id>=9`.
- **Verification:** CONFIRMED (V, verifier).
- **Decision:** **FILE**, `priority:high` · **M30** · dedupe: #1774 / #1778 (image-block family). Sequence it **after** F2: fixing it first ships editions 14-16 as written.

**F7. Celery SUCCESS hides failed work, so the failure instrument is blind.** **S1** · G5, G8
- **Statement:** the window has 0 FAILURE and 0 RETRY across 20,841 runs. The same instrument recorded 18 FAILUREs between 07-04 and 09-05, so it works, but tasks catch the error and return a string. Examples:
  - `auto_publish_edition`: 2/2 SUCCESS with 0 published.
  - `engage_with_profile_viewer`: 100 SUCCESS against 40 engaged failures.
  - `send_scheduled_dm`: 311 SUCCESS inside the orphan loop.
  - 4 "DM Failed" results as SUCCESS.

  The Celery-failure alert tile cannot fire.
- **Evidence:**
  - C-3.
  - A FL-1 (`a_flower.py`: every state is SUCCESS over 26,137 retained tasks).
  - Code: `engage_with_profile_viewer` and `auto_publish_edition` catch and return a string.
- **Verification:** CONFIRMED (V, verifier, code).
- **Decision:** **FILE**, `priority:high` · **M30**.

**F8. A meeting ask and a fabricated claim went out in a DM; the DM surface is ungated.** **S1** · G2
- **Statement:** nurture DM 37 (sent 2026-09-16 00:03:08, logs 1303) to Raimund Laqua read: "…DoD LLM rollout that cut false positives 30% … Do you've 15 minutes for a quick call to swap ideas?". Failed attempt 1297 read "happy to jump on a brief call". The `meeting_cta` gate has 0 hits because it runs on posts only. The CLAUDE.md content-mix invariant bans meeting asks.
- **Evidence:** `SELECT id,status,updated_at,message FROM scheduled_dms WHERE id=37`; logs 1297 and 1303.
- **Verification:** CONFIRMED (V, lead). The verifier's REFUTED verdict was on the wrong row.
- **Decision:** **FILE**, `priority:high` · **M30**.

**F9. The nurture DM backlog was released as a stale burst.** **S1** · G3, G4
- **Statement:** 6 nurture drafts scheduled 09-07..09-13 were re-queued as orphans and deferred on the DM cap about 45 times each: 228 requeue lines, 304 defers and 311 tasks from 09-15 16:40 to 09-16 00:02. At the UTC rollover, 5 were sent and 1 failed within 2m33s, 3-9 days late. Pacing does not space sends inside a run, which also shows up as 7 catch-up DMs in 4m50s on 09-21 (A-10). The name parser produced recipients "to", "on" and "liked", and the "Hi Giri" draft to Saikumar. Those drafts were not sent (B-14).
- **Evidence:** A DB-3 (`a_dm.py`); LOG 09-15 16:40 → 09-16 00:02; DB-2 rows 1379-1385.
- **Verification:** CONFIRMED (V, verifier: 5 DMs 00:00-00:02, the "Hi Giri" draft). Who canceled 5 drafts at 09-15 16:36 is not attributable.
- **Decision:** **FILE**, `priority:high` · **M30** · dedupe: #2078 (orphan DM requeue). Comment there with the burst and staleness evidence.

**F10. Company-page invites: 0 in 14 days behind a misread 0/0 credit count.** **S1** · G3, G8
- **Statement:** lane sessions read "Credits available: 0/0" 15 times and treat that as `credits_exhausted`. Live-Validation sessions on the same days read 50/50: at 09-14 06:44/06:59 and 09-21 06:44/06:53, while the lane read 0/0 at 09-14 16:31 and 09-21 15:59. An unreadable reading is being treated as "no credits". All lane sessions ran with images blocked.
- **Evidence:** `grep -h "Credits available" /opt/lem/logs/cqc_lem_2026_09_*.log`.
- **Verification:** CONFIRMED (V, lead: 15× 0/0, 4× 50/50).
- **Decision:** **FILE**, `priority:high` · **M30** · dedupe: #1774 family.

**F11. The home-feed lane commented on 0 posts in 14 days.** **S1** · G3, G1
- **Statement:** all 15 home-feed scans (the runs outside 17-22h UTC) show "passed filters 0" and 0 comments. The 67 feed comments all came from 54 evening group-feed scans. `feed_fallback_when_empty=1`, yet the fallback never engaged (fallback=False 69/69).
- **Evidence:** `grep -h "Engagement scan:" /opt/lem/logs/cqc_lem_2026_09_*.log`, split by hour.
- **Verification:** CONFIRMED (V, lead). The earlier v_summary pass said "PARTIAL".
- **Decision:** **FILE**, `priority:high` · **M30** · dedupe: the open topic-gate work (memory note `engagement-diagnosis-2026-09`).

**F12. The connection-request lane is idle, and the one invite sent is missing from its ledger.** **S1** · G3, G8
- **Statement:** the last `connection_requests` row is from 2026-09-07 14:03. `scan_connection_candidates` returned "No connection candidates found" 14/14 times and `auto_check_connection_requests` returned "No Connection Requests to Send" 4052/4052, despite `max_invites_per_day=10` and `auto_approve`. The only invite (09-22 11:57, confirmed on-page, via the profile-viewer path) has no ledger row, although Redis `pacing:used:1:2026-09-22` shows invite=1 (L-2).
- **Evidence:** `SELECT MAX(created_at) FROM connection_requests`; A FL-2; logs row 1399.
- **Verification:** CONFIRMED (V, verifier). L-2 is single-source (A).
- **Decision:** **FILE**, `priority:high` · **M30**.

**F13. The appreciation lane delivers nothing and reports success.** **S1** · G3, G8
- **Statement:** 0 `appreciation_touches` rows. "Found 0 recommendation(s)" 38/38 reads. 0 pending invitations in 43 reads. `automate_appreciation_dms_for_user` returned "Appreciation DMs Sent" 44/44 times. All 46 sessions ran `images=blocked`, the likely but unproven reader cause. `APPRECIATION_SOURCES_ENABLED=true`.
- **Evidence:** A L-3 (`a_flower2.out`); `grep -c "Found 0 recommendation"` → 38.
- **Verification:** CONFIRMED (V, lead + verifier).
- **Decision:** **FILE**, `priority:high` · **M30** · dedupe: #1774 family.

**F14. Most "landed" comments can't be verified, and 30% of the checkable ones are gone.** **S1** · G3, G8
- **Statement:** 63/90 third-party comments (70%) are keyed by a `feedpost://` hash with no permalink. They are never outcome-checked and cannot be spot-checked. Of the 20 outcome reads that returned a verdict, 6 (30%) read `comment-removed`. The outcome sweep and the follow-up sweep ("0 ours") independently agree on 1196, 1255, 1277, 1309 and 1375. All 14 visible comments have 0 likes and 0 replies.
- **Evidence:** `SELECT status,COUNT(*) FROM comment_outcomes WHERE created_at>='2026-09-10' GROUP BY status`; `grep "Follow-up: .* comment box(es)"`.
- **Verification:** CONFIRMED (V, lead: 63 hash + 27 URN; 14 checked, 6 removed, 1 unreadable, 1 post-unavailable).
- **Decision:** **FILE**, `priority:high` · **M30**.

**F15. The follow-up ledger claims sends that never happened, and the "breakdown" is empty.** **S2** · G3, G8, G2
- **Statement:** followup rows 1231, 1234 and 1237 (09-12) are `success`, but the placeholder guard **refused** each send ("unfilled_placeholder"), so no "[link]" DM reached anyone (V's correction to B-5). The run result said "Sent 5" when 2 landed. Separately:
  - 5 sent messages promise "a quick summary/breakdown" and attach only the blog index.
  - 5 recipients got near-identical "no worries if the timing…" notes on consecutive days (adrianmizzi, sanusi-dima, deepakkumar101, mohit-harpalani, yavuzyildiz).
- **Evidence:** `SELECT id,created_at,result,message FROM logs WHERE action_type='followup' AND created_at>='2026-09-10'`; LOG 09-12 12:32:19, 12:34:58, 12:36:57.
- **Verification:** CONFIRMED (V, verifier; S-DB + S-LOG).
- **Decision:** **FILE**, `priority:medium` · **M31**.

**F16. The quality trend line counts our own comments as engagement.** **S2** · G8, G1
- **Statement:** `content_quality_scores.engagement_rate` includes own seed and second-wave comments. Post 108 scores 0.444 with 0 third-party engagement (comments=2, own=2, reactions=0), and post 102 scores 0.385. The correct `third_party_engagement_rate` already exists on post_outcome but is not read. This is the #630 trend line, so it reads inflated.
- **Evidence:** `SELECT ref_id,engagement_rate FROM content_quality_scores WHERE surface='post' AND ref_id IN (102,108)`; B `a8.py`.
- **Verification:** two gatherers (B, C) on S-DB. Not in V.
- **Decision:** **FILE**, `priority:medium` · **M31**.

**F17. The cost instruments disagree by about 13×.** **S2** · G6, G8
- **Statement:** the ledger and `llm_call` price by tier and report $4.97 of comment spend. `$ai_generation` prices by provider and reports about $0.46 in total, because `lem-medium` is served by Ollama gpt-oss:120b at $0. The gross_margin_floor alerts are built on the tier price, and MRR reads $79 in the alerts vs $199 in `margin --daily-json`. 104/110 ledger rows have NULL `task_name`. For scale: $9.03 total in the window, and about 20 LLM calls per landed comment.
- **Evidence:** C-6; `SELECT SUM(cost_usd),COUNT(*),SUM(task_name IS NULL) FROM cost_ledger WHERE incurred_on>='2026-09-10'`; PostHog HogQL on `$ai_generation`.
- **Verification:** single gatherer (C), two sources (S-DB + S-PH). Not in V.
- **Decision:** **FILE**, `priority:medium` · **M31**.

**F18. The nightly post-stats scrape loses rows to Chrome OOM.** **S2** · G5, G8
- **Statement:** Chrome hit the memcg OOM limit around 23:02 on 09-21, 22 and 23 (nodes 1, 5 and 9, 1.5 GiB): "Browser tab crashed after 7/4/5 of 13/13/12". post_stats rows fell from 14 a night to 7, 4 and 5. The once-daily warning never escalates.
- **Evidence:** `grep -n "tab crashed" /opt/lem/logs/cqc_lem_2026_09_2[1-3].log`; `SELECT DATE(captured_at),COUNT(*) FROM post_stats GROUP BY 1`.
- **Verification:** C (S-LOG + S-DB). A counts the same 3 nights (A §4a).
- **Decision:** **FILE**, `priority:medium` · **M31**.

**F19. The content plan and mix contract are not met.** **S2** · G2, G1
- **Statement:**
  - The 17 window posts split 35/41/24 value/authority/promo against 70/20/10.
  - Only 1 of 7 generated posts matches its day-type archetype.
  - Promo posts 100 and 110 have no artifact CTA.
  - The Saturday slot is never laid. After "dropped 20 planned slot(s)" on 09-12, the plan has logged "over 30 days out, Skipped" daily from 09-13 to 09-24, and delivers 2 posts a week against `posts_per_week=3`.
  - Posts 101 (Fri) and 102 (Mon) went out on days outside `posting_days`.
- **Evidence:** B `days.py`; `grep "Content Plan" /opt/lem/logs/cqc_lem_2026_09_*.log`; `SELECT content_mix,COUNT(*) FROM posts … GROUP BY 1`.
- **Verification:** B (S-DB + S-LOG). Not in V. The prior value of `posting_days` is unmeasured.
- **Decision:** **FILE**, `priority:medium` · **M31**.

**F20. The contraction pass corrupts grammar.** **S2** · G2
- **Statement:** `content_alignment.apply_contractions` maps "you have" → "you've" and "here is" → "here's" with no context check. The broken forms shipped in sent DMs 1299 and 1303 ("Do you've 15 minutes"), follow-ups 1232/1233, NL18 (4 instances) and pending DM 43 ("you've to be a coder").
- **Evidence:** `PYTHONPATH=src python3 -c "import cqc_lem.utilities.ai.content_alignment as c; print(c.apply_contractions('Do you have 15 minutes?'))"` → "Do you've 15 minutes?".
- **Verification:** CONFIRMED (V, verifier).
- **Decision:** **FILE**, `priority:medium` · **M31**.

**F21. A vision-rejected image was published.** **S2** · G2
- **Statement:** post 100's image `img_4b6d5791c8be.webp` has brief receipt `gate_verdict: "rejected"`, as does the first candidate `img_14fdb434d782`. After max attempts the avatar path returns the last candidate regardless of verdict. The file was purged at publish, so its pixels cannot be audited.
- **Evidence:** `cat scratchpad/media/p100/img_4b6d5791c8be.brief.json`; `image_gen.py:458-491`; `SELECT image_url FROM posts WHERE id=100`.
- **Verification:** CONFIRMED (V, lead).
- **Decision:** **FILE**, `priority:medium` · **M31** · `risk:product-decision`, because fail-OPEN is the documented posture of `render_image_gated`. Owner decides whether a *rejected* verdict should hold the post.

**F22. Group commenting runs out of time.** **S2** · G3, G5
- **Statement:** "Group commenting ran out of time" in 11/14 runs, reaching only 4-5 of 25 groups per run, plus 2 tab crashes (09-13, 09-15).
- **Evidence:** A FL-3 (`a_flower3.out`); `grep -c "ran out of time" /opt/lem/logs/cqc_lem_2026_09_*.log`.
- **Verification:** A (Flower + S-LOG). Not in V.
- **Decision:** **FILE**, `priority:medium` · **M31** · dedupe: #1719 (group timeouts).

**F23. The replies/mentions reader drifted and went blind.** **S2** · G3, G8
- **Statement:** "Mention card walk matched nothing while the page still renders cards" appeared 9× on 3 post days (2 ERROR). There were 0 reply rows in 14 days. That count is consistent with there being no third-party commenters, but the reader cannot prove it. This is a regression after #1985 closed: the drift came back on 09-14 and 09-22 (C-8).
- **Evidence:** `grep -n "Mention card walk matched nothing" /opt/lem/logs/cqc_lem_2026_09_*.log`; `SELECT COUNT(*) FROM logs WHERE action_type='reply' AND created_at>='2026-09-10'` → 0.
- **Verification:** A and C (S-LOG + S-DB).
- **Decision:** **FILE**, `priority:medium` · **M31** · dedupe: reopen or comment on #1985.

**F24. The error→issue cron skips regressions of closed issues.** **S2** · G8, G5
- **Statement:** when a fingerprint maps to a CLOSED issue, the cron files nothing. The mention-card drift returned after #1985 closed, and the group timeouts after #1719, and neither was re-filed.
- **Evidence:** C-8; the cron's dedupe query against `gh issue view 1985 --json state`.
- **Verification:** single gatherer (C), two sources (S-LOG + S-GH). Not in V.
- **Decision:** **FILE**, `priority:medium` · **M31**.

**F25. The roster lanes are starved of input.** **S2** · G3, G1
- **Statement:** about 1 roster comment per day. "No commentable on-topic post found for roster target" 144×. `connect_status` is unknown on 82/82 targets, and 0 escalation transitions happened because blocked is 0 in every scan. 2 follows. The follow-control selector drifted 3× on 09-13.
- **Evidence:** `grep -c "No commentable on-topic post found for roster target"`; `SELECT connect_status,COUNT(*) FROM engagement_targets GROUP BY 1`.
- **Verification:** A (S-LOG + S-DB). Not in V.
- **Decision:** **FILE**, `priority:medium` · **M31** · dedupe: the open roster-starvation work (memory note `engagement-diagnosis-2026-09`).

**F26. Host crons run from a stale checkout.** **S2** · G5, G7
- **Statement:** `perf_snapshot.sh` and `weekly_sdui_drift_check.sh` run from the shared main checkout, which is 31 commits behind origin/main. #2069 was therefore not in effect for the 09-21 SDUI sweep. The F1 probe is the same class of problem: host-side code outside the release train.
- **Evidence:** `git -C /home/lem/linkedin_engagement_manager rev-list --count HEAD..origin/main` → 31 (C-12); `crontab -l`.
- **Verification:** single gatherer (C), two sources (git + crontab). Not in V.
- **Decision:** **FILE**, `priority:medium` · **M31**.

**F27. Reach and engagement are flat, and no network loop has input.** **S2** · G1
- **Statement:**
  - Third-party ER is 3/408 = 0.74% on n=6 posts, against a verified baseline of 0.51% (6/1,177). n=6 is too small to call a change.
  - Mean impressions per post fell from 98 to 68.
  - Followers went 4,279 → 4,317 (+38, +0.9%) and profile views 363 → 431.
  - 0 author replies on 14 checked comments. 0 `post_engagers`. 0 lead-magnet deliveries ever.

  This is why reciprocity, the CTA loop and replies score O0-2: they have no input. By their invariants that is an audience fact, not a lane defect.
- **Evidence:** B `a8.py` over `post_stats` and `follower_stats`; A MJ-1 (engagers=1 throughout); C suppression readings (unverified).
- **Verification:** B (S-DB) and A (S-PERF) agree on engagers.
- **Decision:** **FILE**, `priority:medium` + `risk:product-decision` + `needs-human` (strategy, not code) · **M31**.

**F28. Published and imminent media are misaligned.** **S2** · G2. **FILE** (I-17)
- **Statement:**
  - Deck 105 is "AI in E-commerce: 5 Key Trends" on a governance post, with a stock photo carrying CJK text.
  - Deck 110 promises 5 gates and shows 4. Its caption says "exact 5 checks", and it **publishes 2026-09-24 16:38 UTC** (V).
  - Video 101 is generic avatar b-roll with no captions (`caption_text` NULL; the prod `VIDEO_CAPTIONS_ENABLED` value is unmeasured).
  - The stat_reveal "?" glyph overlaps the title on 103 and 108.
- **Evidence:** `scratchpad/media/c105`, `c110/slide_0[1-6].png`, `v101`.
- **Verification:** two sources: the rendered slides were viewed twice, by gatherer B and again by the gauntlet builder (S-ASSET), and `posts.caption_text` is NULL for 101 (S-DB). The owner explicitly asked for media alignment to be audited, so this is promoted to **FILE** (I-17), `priority:medium` · **M31**. **Owner action today:** check deck 110 before 16:38 UTC.

**F29. Comment voice breaks the owner's own style rule.** **S2** · G2. **FILE** (I-28)
- **Statement:**
  - 13/102 comments use "hit(s) home" or "game-changer", though `comment_style` explicitly bans "hits home".
  - 33/102 contain a non-sequitur filler sentence ("It works.", "It worked well.").
  - The likely source is the humanizer's "≤6 words" sentence instruction (`content_alignment.py:1387`). That link is unproven.
- **Evidence:** B `gram.py`, `sample.py` (seed 20260924).
- **Verification:** the shipped comment text (S-DB `logs`) is set against the owner's own `engagement_preferences.comment_style`. The comments unit scores Q≤2, which meets the ≤2 rule, so **FILE** (I-28), `priority:medium` · **M31**.

**F30. The nightly slow-test lane has failed for 24+ nights.** **S2** · G7. **WATCH**
- **Statement:** `test_baseline_matches_the_real_count` fails 2617 vs 0. Not a required gate.
- **Evidence:** `env -u GH_TOKEN gh run list --workflow slow-tests.yml`.
- **Verification:** S-GH run history (C-9). The fix is cheap and the lane is red every night, so **FILE** (I-26), `priority:medium` · **M31**.

**F31. The suppression tripwire reads immature impressions, so it sits at a noisy `watch`.** **S3** · G4, G8. **WATCH** (resolved by the lead after gauntlet round 1)
- **Statement:** the readings stayed at `watch` for 9 days at a 75-91% impression drop and never tripped. The lead read `suppression.py:_reach_signal` (origin/main):
  - `watch` means **some** of the last 3 posting days dropped by at least the ratio.
  - `tripped` requires **all** 3.

  The 75-91% drops come from posts read while still young (post 108 at 14 impressions, 1.5 days old; post 102 at 25) compared against a baseline median of 87 built from mature posts. Mature window posts sit at 67-160. The tripwire behaved to its contract, and there is **no evidence of sustained suppression**. This matches F27, where mean impressions per post went from 98 to 68 (about 30%, n=6).
- **Evidence:** `git show origin/main:src/cqc_lem/utilities/suppression.py` lines 260-322; B §4 post_stats table.
- **Decision:** **WATCH**. The age bias goes into I-29 as a scope note: compare posts at equal age, or exclude posts under 72 h.

**F32. The margin series in metrics.jsonl is null.** **S3** · G6, G8
- **Statement:** margin is null on 48/63 lines since 08-07. `perf_snapshot.sh` execs python in `web_app`, which is now nginx. `web_api_blue` works.
- **Evidence:** C-7; `grep -c '"margin": null' /home/lem/perf-tracking/metrics.jsonl`.
- **Decision:** **FILE** (trivial fix), `priority:low` · **M31**.

**F33. The "Daily comment budget spent (cap 20)" message is misleading.** **S3** · G8
- **Statement:** 124 lines on 3 days that had only 2-6 comments. The pacing draw, not the cap, was exhausted.
- **Evidence:** `feed.py:2727-2733` (CODE-1); RD-1 `pacing:used`.
- **Decision:** **FILE** (trivial text fix), `priority:low` · **M31**.

**F34. Audit trail and ledger blind spots.** **S3** · G8. **WATCH**
- **Statement:**
  - `follower_stats.connection_count` is pinned at 500, and `search_appearances` has been NULL since 08-28.
  - `purge_post_assets` removes published media, so 5/6 posted items cannot be re-audited.
  - `logs.action_type` has no invite, follow or group type.
  - Group-feed and home-feed comments are indistinguishable in the logs.
- **Evidence:** B-13; A-14.
- **Verification:** WATCH. The enum change needs a migration, so not trivial.

**F35. Releases were held and deployed by hand.** **S3** · G7. **WATCH**
- **Statement:** v0.173.13, v0.174.0 and v0.176.0 were held from auto-deploy. v0.173.13 took 9.5 h to reach prod. One SSH-timeout redeploy failure.
- **Evidence:** C-11.

**F36. Isolated single-run failures.** **S3** · G5. **WATCH**
- **Statement:**
  - Post 100 went out 9h48m late (22:20 ET) through the orphan re-queue (B-15).
  - Group post 09-15: "Group share box not found".
  - One carousel pydantic parse failure (09-18).
  - YouTube invalid_grant on both weekly checks, already tracked in #1094. Comment there.
- **Evidence:** A §4a; B §5.

**F37. Minor account-hygiene notes.** **S3** · G4. **WATCH**
- **Statement:**
  - 33 Live-Validation sessions ran on the prod account outside automation, 24 of them on 09-14 in about 2 h.
  - A contact's first name appears in a WARNING line shipped to PostHog Logs.
- **Evidence:** A §3; C *Security*.

---

**Totals: 37 findings.** Severity: 4 S0, 10 S1, 16 S2, 7 S3 (F31 was re-scoped after gauntlet round 1).

**29 are FILE:**

| Severity | FILE | Milestone | Priority |
|---|---|---|---|
| S0 | 4 | M30 | critical |
| S1 | 10 | M30 | high |
| S2 | 16 (F15-F30) | M31 | medium |
| S3 | 2 (F32, F33) | M31 | low |

**5 are WATCH:** F31 and F34-F37. The authoritative filing list is the §Issue backlog index: 33 issues, because some findings split into several issues and some WATCH items were merged in.

**Units scoring ≤ 2 with no fileable finding of their own:**
- Reciprocity and the CTA loop are covered by F27.
- Stale-invite withdrawal: O2, and its 2 budget_reached / 0 withdrawn result is unexplained. Single-source, WATCH.
- Media: covered by F21 and F28 (both FILE).


---

## 6. Artifact quality and media-to-content alignment review

Window 2026-09-10 00:00 UTC → 2026-09-24. Every artifact LEM generated or sent under the owner's
name in the window was read in full, except where §1 says otherwise. Sources: prod DB read-only
(S-DB), `/opt/lem/logs` (S-LOG), the `lem_assets` volume copied to the scratchpad (S-ASSET). Query
and analysis scripts (`q1-3.py`, `sample.py`, `pairs.py`, `nlscan.py`, `lint.py`, `gram.py`,
`days.py`, `a8.py`) are in the audit scratchpad. The P2 verification ledger overrides the gatherers
wherever the two disagree.

The deterministic gates (`slop_lint`, the #617 comment contract, `evaluate_post_gates`, the image
vision gate) answer one question: *did this draft do anything forbidden?* This section asks two
others. **Q (craft, 0-4):** would a LinkedIn reader stop for this? **A (alignment, 0-4):** does it
match the owner's voice, the story bank's facts and the content-core contracts?

Owning docs: `docs/content-core.md`, `docs/image-stack.md`, `docs/content-scheduling.md`. The voice
target is quoted from `engagement_preferences` and `profiles.synthesis` (profile 7):

> tone *"direct, warm, credible, plainspoken — a practitioner, not a pitch"* · comment_style
> *"Lead with a specific point … Add one concrete insight from real LLM-ops experience … No
> buzzwords (i.e "hits home") … never pitch"* · focus_topics: LLM cost, routing, reliability,
> governance · business_goal *"Earn 20-min diagnostic conversations"* · `posting_days` [1,3,5],
> `posts_per_week` 3, `hold_repaired_posts_for_review`=1, `use_emojis`=1.

The **story bank** holds 7 active entries: 160 releases in 32 days; the 429 doom loop; cost-aware
down-routing (shipped dark, **no result numbers**); the model-retirement cron; the VARCHAR(64)
rollback; blue/green deploys without k8s; 369 PRs, 41 of them in one day. **It contains no client
engagement, no cost-savings percentage and no e-commerce deployment** (S-DB `story_bank`, 7 rows,
0 mention "client").

**Headline.** The system writes competent-looking text about things that never happened. Every
surface that publishes or sends shipped an invented first-person fact in the window: 3 of the 6
posted posts, the one published group post, a sent DM, and 24 of 102 comments. 6 of the 7
newsletters also contain invented client stories; three of those are approved and queued to
auto-publish, and only a broken editor selector is holding them back. The checks that should stop
this cover posts and comments only (`content-core.md` names newsletters, group posts and DMs as "not
yet covered"). Where the checks did fire, three different routes carried the artifact past them: a
heal path, an unrecorded approval, and a gate that returns its best rejected candidate. The media
largely tracks the text, but where it fails, it fails visibly. The worst case publishes today:
deck 110 promises 5 checks and shows 4.

---

### 6.1 What was and was not sampled

| Asked for | What was actually available | Why |
|---|---|---|
| Every post in the window | **7 bodies read in full**: 6 posted (100, 101, 102, 103, 105, 108) plus 110 (approved, publishes 2026-09-24 16:38 UTC). The 10 `planning` rows (113-135) were **not graded** | Planning rows are future calendar slots, not shipped text |
| Every newsletter edition | **7 of 7 touched editions read in full** (11, 13-18; 3.9k-6.0k chars each). **0 published in the window** | The publish lane failed 2 of 2 times (P2 C-2) |
| Group posts | **2 of 2** (6 published 09-22, 5 failed 09-15). **Group post media not measured** | The draft row was read; no asset was pulled |
| DMs | **All bodies read**: 65 `dm` success and 4 failure logs, 13 `followup` rows, 19 `scheduled_dms` | — |
| Comments | **25 of 102 hand-graded** (random, `random.seed(20260924)`). **Regex and slop-lint counts over all 102** | Hand-grading all 102 was out of scope. The regexes size the population |
| On-platform state of comments | **Not checked.** 63 of the 90 third-party comments are keyed `feedpost://<sha1>` with no permalink | No URL means no way to revisit the comment. Only the 20 URN-keyed rows reach `comment_outcomes` |
| Post images 100 and 102 (pixels) | **Brief receipts only.** Both files were purged at publish | `purge_post_assets` deletes a published post's media by design (`image-stack.md` §"A published post's local asset is NOT retained") |
| Video 101 | **Three frames (open, mid, close), `probe.json` and the brief.** The MP4 was purged | Same purge |
| Decks 103, 105, 108 | **Keyframes of slides 1-2 plus `deck_render.json`.** Slides 3 onwards were never seen | The published PNGs were purged |
| Deck 110 | **All 6 PNGs viewed by this reviewer** | Not yet published, so not purged |
| Newsletter covers ed13-ed18 | **All 6 PNGs present with their briefs.** This reviewer re-viewed ed14, ed17 and ed18 directly; ed13, ed15 and ed16 are graded as P1-B viewed them | — |
| Newsletter cover ed11 | **Not reviewed.** Rendered 2026-08-12, before the window | Its edition's publish failed in the window, but the render is out of scope |
| **Avatar likeness** (post 100 image, video 101) | **Not measured** | No owner reference image was compared, and the post 100 file is purged |
| **`VIDEO_CAPTIONS_ENABLED`, prod value** | **Not measured.** The code default is `False` (`env_constants.py:116`), and it is also a PostHog flag (`flags.py:164`) | The `.env` read was denied, and the PostHog key and MCP were unavailable to this audit. What *is* measured: post 101 has `caption_text`/`caption_srt_url` NULL and there are 0 "caption" log lines in the window |
| Who approved post 105 | **Not recorded anywhere.** `approvals` has 0 rows for window posts; there are 0 approval log lines 09-14..09-17 | There is no actor column (theme T4) |
| A real, fetched LinkedIn exemplar | **Not fetched.** Graded against the rubric plus the owner's own `engagement_preferences` | Fetching one needs a live authenticated session, and this audit was read-only |

**Read the scores as sizing, not calibration.** Seven posts and 25 comments from one account can
show that a gap exists. They cannot set a threshold.

---

### 6.2 Per-surface verdicts

Surface scores are the reviewer's overall grade, not an arithmetic mean. A surface that published
one invented fact cannot score above 1 on A, however clean the rest of it is.

| Surface | n | Q | A | One-line verdict |
|---|---|---|---|---|
| Posts | 7 | **2** | **1** | The best two (103, 108) are grounded and decent. Three posted posts carry invented results, and one shipped with a HARD slop hit after being held |
| Newsletters | 7 | **1** | **0** | 6 of 7 invent a client or first-person incident. 3 are approved and will auto-publish |
| Group posts | 2 | **2** | **0** | Both invent a "40% per-call cost" result. One is published |
| DMs: catch-up | ~41-45 | 2 | 3 | Templated but harmless |
| DMs: profile-viewer cold | 5 sent | 3 | 3 | The best surface. The soft ask "I'm happy to chat" is acceptable |
| DMs: follow-up | 13 rows (10 landed) | **0** | **1** | Promises a "breakdown" and attaches nothing, double-nudges, and records false success (§2.4) |
| DMs: nurture | 5 sent | **1** | **1** | One sent DM combines a meeting ask, a fabrication and broken grammar (DM 37) |
| Comments | 102 | **1** | **1** | Sample mean Q 1.28 / A 1.52. 22 same-post double comments, 24 first-person numbers, 13 banned buzzwords |

#### 2.1 Posts (all 7)

| id | status | mix / stage / archetype (day-type fit) | slop rerun | Q | A | Evidence (quoted) |
|---|---|---|---|---|---|---|
| 100 | posted 09-11 02:20 UTC, 9h48m late (orphan re-queue) | promo / awareness / case_snapshot (Thu wants contrarian_take or myth_vs_reality ✗) | pass | 2 | 2 | Story 3 embellished: *"During a high-traffic e-commerce cycle, we needed to manage token usage without impacting checkout speed"*. The bank has no e-commerce event. *"An auto-rollback trigger reverted the change immediately"* turns a capability into an event. **Promo post with no artifact CTA** |
| 101 | posted | value / awareness / myth_vs_reality (Fri is outside `posting_days` ✗) | WARN rhetorical_hook | 1 | 1 | *"cut your AI spend by nearly half"*, *"shaving a double-digit percentage off the monthly bill. The trick was just a simple latency-threshold rule"*. Story 3 shipped dark with no results. The repair pass fired (09-11 11:28, *"unsourced first-person specifics (46)"*) and the invented result shipped anyway. *"I've put together a quick checklist"* has no delivery path |
| 102 | posted | value / consideration / how_to (Mon is outside `posting_days` ✗; the day type matches) | pass | 2 | 2 | Unsourced figures: *"cut your AI spend by 60%"*, *"up to 70%"*, *"hit the cache 90% of the time"*, *"prices vary by 90x"*. Story 7 conflated with cost work. The AUDIT CTA is valid |
| 103 | posted | authority / decision / question_starter (Tue wants case_snapshot or how_to ✗) | WARN burstiness | **3** | **3** | Story 1 accurate. Weak spot: seven stacked CTA lines at the end |
| 105 | posted 09-17 **after a HOLD on 09-13** | value / awareness / industry_observation ✗ | **HARD banned_lexicon** (advancements, robust, landscape) | **0** | 1 | *"AI is transforming e-commerce faster than ever … unprecedented opportunities for efficiency and growth"*. Authenticity 55, under the 60 floor. Off every focus topic. No approver recorded |
| 108 | posted, **auto-approved by the heal path** | authority / decision / contrarian_take ✗ | WARN burstiness | **3** | **3** | Story 6 accurate. Generic padding: *"RAG pipelines need constant uptime during model updates"* |
| 110 | approved, publishes today 16:38 UTC | promo / awareness / tactical_list ✗ | WARN burstiness | 2 | 2 | Story 7 accurate. *"I broke down the exact 5 checks … in the carousel below"*, but the deck shows 4 (§3). The promo CTA is *"Save this checklist"*, not an artifact |

Contract readings across the window's 17 post rows: mix value 6 / authority 7 / promo 4 (**35 / 41 /
24** against 70 / 20 / 10). Day-type archetype match: **1 of 7** (102 only). Meeting CTA in posts:
0. `meeting_cta` holds: 0.

#### 2.2 Newsletters (all 7)

The newsletter slop lint was rerun on the newsletter surface. Fact grounding does not run on this
surface at all (`content-core.md` §forbidden-claim: *"The newsletter, weekly group post and DM
writers go through the slop lint only — not yet covered."*).

| ed | status | slop (newsletter surface) | Q | A | Fabrication (quoted) |
|---|---|---|---|---|---|
| 11 | failed (publish) | **HARD contrastive_frame** | 2 | 0 | *"It was a Wednesday in February … my three-agent pipeline … polisher_v3 … my monthly API bill nearly tripled"* |
| 13 | failed | **HARD contrastive_frame + canned_scaffold** | 1 | 1 | *"One company cut their monthly AI expenses from USD 10,000 to USD 3,200"*, unsourced. A generic listicle |
| 14 | **approved**, due 09-29, cover `pending_review` | pass | 2 | **0** | *"I audited a pipeline last quarter where 80% of tokens went to a frontier model … **That's a real number from a real client.**"* |
| 15 | **approved**, due 10-06 | pass | 2 | **0** | *"Last October, I audited a client's content engine during a Q4 budget review"* |
| 16 | **approved**, due 10-13 | **HARD contrastive_frame** | 1 | **0** | *"I signed a check for USD 12,000 last month for AI tools we didn't need"*, *"a B2B consultancy client approached me"* |
| 17 | draft | WARN burstiness | 2 | 0 | *"forty drafts generated for a Fortune 500 retail client last quarter"* |
| 18 | draft | pass (the lint misses the grammar) | 1 | 0 | *"we cut their inference spend by half"*. Grammar: *"here's like hiring a surgeon"*, *"if you've a carousel draft"* (×3) |

Edition 16 is approved even though it carries a HARD slop finding on its own surface.
`auto_publish_newsletters`=1, so **fixing the editor selector (P2 C-2) publishes 14, 15 and 16 as
written**. See T1.

#### 2.3 Group posts

| id | status | Q | A | Quote |
|---|---|---|---|---|
| 6 | **published 09-22** in "Analytics and AI in Marketing and Retail" | 2 | 0 | *"I've seen a 40% drop in per-call cost by routing 80% of queries to a distilled LLM … monitoring confidence scores in real time"* |
| 5 | failed (share box not found) in an ABA bank group | 2 | 0 | *"we've cut per-call cost by ~40% while keeping false-positive rates low"* in *"AI-driven fraud screens"*. The owner has no fraud product |

#### 2.4 DMs by class

| Class | Sent in window | Q | A | Evidence |
|---|---|---|---|---|
| Catch-up congrats | ~41 per P1-B (45 attempts = 45 sent per P1-A) | 2 | 3 | *"Congrats on …"*, *"happy birthday"*. Templated but acceptable. Pacing issue out of scope here: 7 sent in 4m50s on 09-21 |
| Profile-viewer cold DM (`profile_viewer_dm_auto_send`=1) | 5 sent (logs 1263, 1283, 1284, 1397, 1398), 1 failed | 3 | 3 | *"I'm happy to chat"*: a soft ask, within the ARTIFACT-CTA spirit only loosely |
| Follow-up step | 13 `followup` rows, **10 landed** | **0** | **1** | **Corrected per P2 B-5:** the three *"here's a quick breakdown: [link]"* rows (1231, 1234, 1237, 09-12) were **refused by the placeholder guard and never sent** (LOG 09-12 12:32:19 / 12:34:58 / 12:36:57 *"Refusing to send an unsendable DM … unfilled_placeholder"*). The defect is that the ledger row says `success`, and Flower reported *"Sent 5"* when 2 landed. Of what did land, 5 messages promise *"a quick summary for you to check out later:"*, and the link is the blog index, not an artifact. 5 recipients (adrianmizzi, sanusi-dima, deepakkumar101, mohit-harpalani, yavuzyildiz) got two near-identical *"no worries if the timing…"* notes on consecutive days. Grammar: *"when you've a moment"* (1232/1233) |
| Nurture (approval-gated) | 5 sent: 31, 33, 35, 37, 38, **all at 09-16 00:01-00:03, 3-9 days stale** | 1 | 1 | **DM 37 sent 2026-09-16 00:03:08** (log 1303, P2-confirmed): *"Your cybernetic AI work brings to mind a DoD LLM rollout that cut false positives 30% … **Do you've 15 minutes for a quick call** to swap ideas?"*. That is a fabrication, a meeting ask and broken grammar in one sent message (Q0 A0). 33: invented *"cut iteration time about 30%"* (Q1 A1). 31: *"Do you've a particular focus"* (Q2 A2). 35: *"Running a quick RAG sandbox helped teams spot bias early. It works fast."* (Q1 A2). 38: Q3 A3 |
| Nurture, not sent | canceled / failed / pending | — | — | 29: *"I've built production-grade LLM agents inside Workday … payroll checks"*. 30: *"Sounds good, To."* (the name was parsed from "to"; other recipients were recorded as "on" and "liked"). 39: *"Hi Giri"* to Saikumar. Log 1297 (failed): *"I'm happy to jump on a brief call"*. 40 pending: invented *"reduced drift by about 30%"*. 43 pending: *"you've to be a coder"* |

Slop lint on DMs: 0 HARD, 3 WARN. The lint cannot see fabrication, grammar or a meeting ask.

#### 2.5 Comments (102 landed: 90 third-party, 12 on our own posts)

**Population counts over all 102 (regex, `sample.py`):**

| Signal | Count | Rule it breaks |
|---|---|---|
| First-person claim plus a number | **24** / 102 | Fact grounding, HARD on comments (#1834) |
| Non-sequitur filler sentence (*"It works."*, *"That hurts."*, *"Sounds solid."*) | **33** / 102 | Voice: "plainspoken, a practitioner" |
| *"hit(s) home"* or *"game-changer"* | **13** / 102 (ids 1240, 1242, 1243, 1277, 1313, 1315, 1348, 1354, 1375, 1412, 1413, 1423, 1426) | `comment_style` bans "hits home" **by name** |
| Slop lint rerun | 0 HARD, 2 WARN (em_dash) | Agrees with CQS (`slop_hard`=0 on all 105 rows) |
| Same-post double comment (second one 74-98 s after the first, under a different `feedpost://` hash) | **22 pairs = 44 of 67** logged feed comments | P2-confirmed (A-1/B-1). `cqc_lem_2026_09_12.log:583-586` |
| Quality-contract skips (the guard working) | 110, of which 89 were for "first-person specifics" | — |

**Hand sample of 25: mean Q 1.28, A 1.52.** 6 of the 25 are the second comment of a pair. Those six
score Q 0-1 / A 0-1, because they answer LEM's *own* first comment as though it were the author's
post:

- 1367 → **1368**: *"shaved roughly 15% … It saved us."* (Q0 A0)
- 1391 → **1392**: *"15 % latency bump … Sounds solid."* (Q0 A0)
- 1194 → **1195**: *"In our fraud-detection pipeline … It works."* (Q1 A0)
- 1389 → 1390: *"You're spending high-hundreds of thousands per epoch on a 7 B model"*. This restates our own invented claim back as the author's

Other low scores in the sample:

- 1274 (Q1 A1): *"AI should help knowledge workers, not take their jobs. It worked well."*, plus *"errors drop about 25%"* in an invented *"e-commerce LLM rollout"*
- 1348 (Q0 A1): *"That's a game-changer … shaved about 25 % off latency"*
- 1333 (Q1 A1): incoherent and invented (*"cost per call only dropped once we linked usage to a sales-pipeline KPI"*)

The best comment in the sample, 1308 (Q3 A3), is specific, relevant and invents nothing. So the
target is reachable.

Outcome at the comment level: 14 checked outcomes, **0 likes, 0 replies, 0 author replies**. 6
URN-keyed comments read `comment-removed` (P2 A-2).

---

### 6.3 Media–content alignment

Scale: topic match and message support 0-4. "Gate" is the stored receipt (`gate_verdict` in
`*.brief.json`), or n/a where no vision gate runs. Decks use stock photos plus template text and do
not pass `render_image_gated` (`image.md` F4 / #1290).

| Artifact | Paired text | What the reviewer saw | Topic | Supports message | Text / logo artifacts | Gate vs reviewer look | Verdict |
|---|---|---|---|---|---|---|---|
| Post 100 image `img_4b6d5791c8be.webp` (avatar LoRA, 1:1) | Post 100, flexible model routing | **Purged 09-11 02:20:01.** The brief reads *"A mid-30s AI-ecommerce specialist stands beside a sleek server rack, holding a bright orange coffee mug … dark, turned-off laptop"* | unknown | unknown | unknown | **`rejected`**, and so was the first candidate `img_14fdb434d782`. **It shipped** (`render_image_gated` returns the best candidate after `IMAGE_GATE_MAX_ATTEMPTS`, `image_gen.py:453-487`) | **S2**: a rejected render was published and cannot now be re-audited |
| Post 102 image `img_b78e7fcc3d4b.webp` | Post 102, cost checklist | Purged. The brief reads *"AI-e-commerce specialist … holding an open checklist notebook … vivid orange coffee mug"* | plausible | weak (a man with a mug does not show a cost checklist) | unknown | `accepted` | Unverifiable |
| Video 101 (veo3.1, 6 s, 9:16, avatar; $2.40) | Post 101, "cut AI spend" | Mid frame: a bearded man in a charcoal long-sleeve standing in an empty brick loft corridor, hand in pocket. Nothing about cost, routing or spend | **1** | **1** | none | `accepted`, against focal concept *"An AI architect contemplating strategic efficiency"*. Reviewer: generic headshot b-roll, the failure `image_gen.py` itself describes (*"a plain headshot against a brick wall"*) | **S2 misaligned. No captions** (`caption_text` NULL) on a muted-autoplay surface |
| Deck 103 (stat_reveal, 4 slides; keyframes 1-2) | Post 103, speed vs stability, 160 releases | Slide 1 *"Speed Versus Stability: The Real Tradeoff"*. **Viewed: the large "?" glyph overlaps the "y" of Stability and the "ff" of Tradeoff.** Slide 2 sits on a low-res chess-king stock photo | 3 | 2 | layout collision | n/a | S3 layout |
| Deck 105 (bold_listicle, 6 slides; keyframes 1-2) | Post 105, governance / 369 PRs | **Viewed:** cover *"AI in E-commerce: 5 Key Trends … actionable steps for your business"*. Slide 2 *"AI's Growing Role"* over a pixelated white robot photo carrying **visible CJK characters** | **1** | **0** | foreign text in the stock photo | n/a. Slide slop WARN burstiness | **S2**: generic trend deck, off-pillar, published |
| Deck 108 (stat_reveal, 5 slides; keyframes 1-2) | Post 108, zero-downtime without k8s | **Viewed:** cover *"Zero-Downtime Deploys Without Kubernetes — The 3 requirements for safe releases on a VPS"*. "1 of 5 reveals" = cover + 3 bodies + CTA, so the count is consistent. The "?" crowds "Without" but does not overlap it | 3 | 3 | none | n/a. WARN burstiness | OK, minor layout |
| **Deck 110** (bold_listicle, 6 slides; **all 6 viewed**) | Post 110: *"the exact 5 checks … in the carousel below"* | Cover *"The 5 Gates for AI Code — How I merged 41 PRs in one day safely"*. Slides 2-5: **1 Tests and Coverage, 2 Security Scanning, 3 Adversarial Review, 4 Staged Deploys.** Slide 6 recap: *"Tests, Security, Review, Staging, Monitoring"*. **Gate 5, Monitoring, has no slide.** Stock photos are low-res; slide 5 "Staged Deploys" uses an **audio-synth LFO panel** ("LFO Rate", "Low-pass Frequency") | 3 | **2** | no rendered marks; the stock photo UI text is legible but off-topic | n/a. WARN burstiness | **S2: the promise-to-content count mismatch (5 promised, 4 shown) publishes today 16:38 UTC.** `chars_dropped`=0 on every slide, so this is an authoring gap, not a render truncation |
| Cover ed13 (approved) | NL13, auditing AI LinkedIn costs | Analog gauge reading low, e-commerce boxes behind (P1-B view) | 3 | 3 | none | `accepted` after **3 rejected** briefs (09-09) | OK. The edition failed to publish |
| Cover ed14 (**pending_review**; edition approved, due 09-29) | NL14, routing framework | **Viewed:** a red/amber/green traffic signal in a blurred data-center aisle | 4 | 4 | none | `accepted` after **3 rejected** | OK. It will ship unapproved under notify-and-publish (by design) |
| Cover ed15 (approved) | NL15, "spot the leak" | Copper pipe dripping into a tin (P1-B view) | 4 | 4 | none | `accepted`; 2 rejected, 2 accepted | OK |
| Cover ed16 (approved) | NL16, "the USD 30K we nearly squandered" | Cracked gear spilling copper packets (P1-B view) | 4 | 4 | none | `accepted`; 1 rejected, 3 accepted | OK. **The cover is aligned; the text it fronts is fabricated** |
| Cover ed17 (pending_review; draft) | NL17, AI content hallucinates | **Viewed:** a wet brass gate valve dripping in a dark concrete room under one lamp | 2 | 1 | none | `accepted` (*"failing industrial valve … metaphor for AI hallucinations"*) | Weak: a leak reads as "cost leak", not hallucination. It reuses the valve motif of ed13-16 |
| Cover ed18 (pending_review; draft) | NL18, 5 routing rules | **Viewed:** a rusted rail-switch lever on ballast beside diverging track | 4 | 4 | none | `accepted` | OK |
| Cover ed11 | NL11 (failed) | Not reviewed (rendered 08-12, before the window) | — | — | — | — | Out of window |

Readings:

- **No garbled text and no logos in any AI render.** R2 of the image audit holds.
- The **covers are the strongest media in the system**: 5 of 6 score 3-4 on both axes. The gate
  visibly earns its keep there, rejecting 9 briefs across ed13-16 before accepting.
- The **same gate on the post surface let a doubly-rejected render ship** (post 100). That is the
  gap between the two surfaces.
- **5 of 6 posted items can no longer be re-audited from their pixels** (purge by design).
- **E-commerce framing** appears in:
  - image briefs 100 and 102 (*"AI-ecommerce specialist"*)
  - covers ed13 and ed15 (shipping boxes, product shelves)
  - posts 100 and 105, deck 105
  - comment 1274

  None of the owner's focus topics or story-bank entries is e-commerce. Where that framing comes
  from upstream (likely the positioning or profile brief) is **not measured**.

---

### 6.4 Cross-cutting themes

| # | Theme | Severity | Evidence (counts trace to §2/§3) | Content-core invariant it breaks |
|---|---|---|---|---|
| **T1** | **Fabrication outruns the story bank on every surface** | **S1** now. **S0 the moment the newsletter editor selector is fixed**: 3 approved editions carrying "real client" claims auto-publish (`auto_publish_newsletters`=1) | Posted posts 100, 101, 102. Published group post 6. Sent DMs 33 and 37. 24/102 comments. 6/7 newsletters (14/15/16 approved). Story bank: 7 entries, no client work, no savings numbers | **Story bank is the ONE fact layer, "the only permitted specifics"** (`content-core.md` §Story bank). Fact grounding (#1834, HARD on comments; HARD on posts since #1971/PR #2052, merged in the window and **unverified live**) covers posts and comments only. **Newsletter, group-post and DM writers are documented as "not yet covered"**, and those are the surfaces where the worst fabrications sit. On comments the gate is laundered: the second comment of a same-post pair grounds on our own first comment as "the target post", so our invented claim becomes an allowed source (1389→1390, 1245→1246). That mechanism is inferred from text, not verified on-platform |
| **T2** | **Voice-rule violations the gates cannot see** | **S1** (meeting ask sent) / S2 (buzzwords, filler) | Sent DM 37: *"15 minutes for a quick call"*. Failed 1297: *"jump on a brief call"*. 13/102 comments use the explicitly banned "hits home"/"game-changer". 33/102 carry filler non-sequiturs | **"A meeting ask is banned … any that survives HOLDS"** (`content-core.md` §Content mix), but the `meeting_cta` gate runs on posts only (0 holds; 0 hits on DMs). The owner's `comment_style` ban list is not wired into any check. P1-B suspects the filler comes from the humanizer instruction *"mix at least one very short sentence (<=6 words)"* (`content_alignment.py:1387`); **the attribution is unproven** |
| **T3** | **The deterministic contraction pass corrupts grammar** | **S2** | Sent DMs 1299 and 1303 (*"Do you've 15 minutes"*, *"Do you've a particular focus"*). Landed follow-ups 1232/1233 (*"when you've a moment"*). NL18 ×4. Pending DM 43 (*"you've to be a coder"*). Reproduced locally: `apply_contractions('Do you have 15 minutes?')` → *"Do you've 15 minutes?"* | `content_alignment.py` is the ONE alignment core, so one context-free rule (`apply_contractions`, line 1328: *"you have"*→*"you've"*, *"here is"*→*"here's"*) corrupts every surface at once. Slop lint does not check grammar |
| **T4** | **Held artifacts ship anyway: three routes past the gates** | **S1** | (a) **Heal path:** post 108 held 09-18 01:30, then *"healed to approved"* 03:16 by `regenerate_post_carousel_task` (`run_content_plan.py:2023-2027`), with no gate re-check and `gate_reason` left stale. (b) **Unrecorded approval:** post 105 held 09-13, published 09-17 with slop HARD and authenticity 55; 0 approval rows, 0 log lines. (c) **Rejected render:** the post 100 image shipped with `gate_verdict` "rejected" twice. (d) Edition 16 approved carrying HARD contrastive_frame | **"Over the ceiling is ONE retry, then HELD"** (#1452) and `hold_repaired_posts_for_review`=1. The image-stack backstop for post images is *"the queue IS the gate"*, and route (c) is only safe if a human saw the verdict. With no approval actor recorded, that is **unmeasurable** |
| **T5** | **The mix and cadence contract is not met** | **S2** | Mix 35/41/24 against 70/20/10, even though the doc says *"promo can never exceed 10%"*. The promo slot should be a *"forced case_snapshot TEXT post"*, but promo 110 is a carousel tactical_list. Promo 100 and 110 carry no artifact CTA. Day-type match 1/7. 101 (Fri) and 102 (Mon) posted outside `posting_days` [1,3,5], after the 09-10 13:14 prefs update (the earlier value is unmeasured). The Saturday slot was never laid. The plan has been stuck *"over 30 days out"* since 20 slots were dropped 09-12, so it runs 2 posts/week against `posts_per_week`=3 | `content-core.md` §Content mix (`assign_content_mix`, `PROMO_EVERY_N_POSTS` clamp). `content-scheduling.md`: *"`posting_days` is the separate, harder bound"*. `POST_DAY_TYPES` sets each post's archetype |
| **T6** | **Media drifts from its text where no gate looks** | **S2** | Deck 105: generic e-commerce trends over a CJK-text robot on a governance post. Deck 110: 5 promised, 4 shown, today. Video 101: b-roll with no captions. Stat_reveal "?" collision (103) | Decks never pass the vision gate (`image.md` F4, #1290), and nothing compares a post's promised count ("5 checks") against its deck's body-slide count. Captions (#1278) *"fail open"*; whether the flag was simply off is unmeasured |
| **T7** | **This review cannot be repeated** | S3 | 5/6 posted items purged. No approval actor. CQS `engagement_rate` counts our own seed and second-wave comments (108: 0.444 with zero third-party engagement) | `image-stack.md` accepts the purge on purpose. What it does not supply is a published-media audit copy or thumbnail, so G2 grades 4 of 6 published media from receipts and keyframes only |

---

### 6.5 Performance tie-in

| Measure | Window (posts 100-108) | Pre-window baseline | Source |
|---|---|---|---|
| Third-party ER, (reactions + comments − own comments + reposts) / impressions | **3 / 408 = 0.74%** (n=6) | **6 / 1,177 = 0.51%** (posts 88-99, 137) | S-DB `post_stats`, `a8.py` |
| Mean impressions per post | 68 | 98 | same |
| Followers | 4,281 → 4,317 (+38, +0.9%, 09-10 → 09-23) | 4,279 on 09-09 | `follower_stats` |
| Comment outcomes | 14 checked: 0 likes, 0 replies, 0 author replies; 6 removed | — | `comment_outcomes` |
| Lead-magnet deliveries | 0 (0 ever) | — | `lead_magnet_sent` |

**No change can be called.** The window's third-party engagement is **three single reactions**, on
102 (Q2 A2), 103 (Q3 A3) and 105 (Q0 A1). One of the three landed on the worst-graded post, so at
n=6 quality does not visibly predict engagement in either direction. The +0.23 pt ER move is
smaller than one reaction's effect on a 408-impression base, and it comes with a 31% drop in mean
impressions. What *can* be said:

- The artifacts that invent the most (comments, the group post, the newsletters) earned **zero
  measurable response**.
- The only surface-level quality instrument, CQS `engagement_rate`, reads **inflated** because it
  counts our own comments, so it cannot show a quality → engagement link even when one exists.

Re-grade at n≥20 posts, with third-party ER as the dependent variable, once T1 and T4 are closed.


---

## 7. Root causes, improvement plan, and issue backlog

This section draws only on the P1-A, P1-B and P1-C ledgers. Where the P2 verification ledger re-derived a claim, its verdict wins. Finding ids (A-n, B-n, C-n, L-n) point back to those ledgers.

Each claim is marked as either **confirmed** (re-derived by P2 or read directly in code) or **hypothesis** (a likely mechanism not yet proven). A number with no source id is not used here.

### 7.1 Root causes, ranked

The ranking is by harm: account risk first, then published harm, then lanes that deliver nothing, then quality and measurement. Every symptom is grouped under the shared cause that explains it. Fixing the cause clears all of its rows.

1. **Safety controls are being undone from outside the codebase: stale host crons and host tooling (G4, G5, G8).**
   - A July diagnostic cron still runs every day: `0 14 * * * /home/lem/recovery-probe/probe.sh`. Its `probe.sh:25-28` calls `clear_rate_limit()` unconditionally, before its own API call. That call has returned 401 every day, so the probe never disables itself: 63 "probe start" lines (C-1, P2 confirmed).
   - The result: the 429 breaker, the one control that is documented as never being a flag, gets wiped once a day whatever LinkedIn is doing. It has not mattered this window only because 0 × 429s occurred (A-15).
   - The same failure (host scripts that nothing versions or deploys) explains 3 more findings:
     - The perf and SDUI-drift crons run from a main checkout that is 31 commits behind, so #2069 was not in effect for the 09-21 sweep (C-12).
     - `perf_snapshot.sh` execs python in `web_app`, which is now nginx, so margin is null on 48 of 63 lines (C-7).
     - The error→issues cron skips any fingerprint whose issue is CLOSED, so regressions of #1985 and #1719 went unfiled (C-8).
2. **Feed dedup is keyed on a content hash, not on post identity (G3, G4, G2).**
   - 63 of 90 third-party comments (70%) were stored under a `feedpost://<sha1>` key, which has no URN and no URL (A-2).
   - 22 times, a second comment landed on the same group-feed post 74-98 s after the first, under a different hash. Each such pair has the same author and post age, and both comments were logged as success (A-1/B-1, P2 confirmed; example `cqc_lem_2026_09_12.log:583-586`).
   - The second comment usually answers LEM's own first comment: 6 of the 25 sampled comments did this (B-1).
   - Mechanism (**hypothesis**): after we post, the walk re-reads the card and the text node it picks up is now our comment. `_feed_post_key(author, content)` (`feed.py:512-518`) then mints a new key and the fingerprint guard misses it.
   - This is the third appearance of the #474 → #580 defect class.
   - The same missing identity also means that 70% of comments can never be outcome-checked or spot-checked on the platform (A-2).
3. **The pre-post window re-dispatches without per-run idempotency (G4).**
   - On every post day, `automate_commenting` and profile-viewer engagement re-fire about every 85 s for the whole window (A-3, P2 confirmed).
   - Effects:
     - Session density reached 20-41 sessions per 30 min, against a baseline of 9-15.
     - The same viewer was revisited 5-9 times in about 15 min (Deepak Yadav 9× on 09-17).
     - 40 `engaged/failure` rows were written.
     - 32 passes ended "0 comment(s)" (A-3).
     - 124 lines printed a misleading "budget spent (cap 20)" when the real limit was the pacing draw (A-13).
   - Human pacing caps the daily **count** but not session density or spacing within a run. The same gap explains the 7 catch-up DMs sent in 4m50s (A-10).
   - Temporal lead, **not** proven: PR #2033 ("re-arm a lost lifecycle") merged 2026-09-10 18:14 UTC, and the first loop day is 09-11.
4. **Fact and content gates apply to the POST surface only, and several paths skip even those (G2).** The story bank has 7 entries, with no clients, no savings percentages and no e-commerce work (B-1 §1).
   - **Surfaces that have no fact gate:**
     - Newsletters: 6 of 7 carry invented client stories. NL14 says "That's a real number from a real client". NL14-16 are *approved*, `auto_publish_newsletters=1`, and the next one is due 09-29 13:00 (B-2, P2 confirmed).
     - Group post 6 was published with "I've seen a 40% drop" (B-2).
     - Sent DMs made the "DoD LLM rollout that cut false positives 30%" claim, and DM 37 included a meeting ask (B-2, B-4).
     - 24 of 102 comments make a first-person numeric claim (B-2, C-10). That is despite #1834 being closed.
   - **Paths that skip the gates that do exist:**
     - `regenerate_post_carousel_task` sets APPROVED on any non-posted post once real slides exist. It runs no gate re-check (`run_content_plan.py:2023-2027`, read in code), so held post 108 was published unreviewed (B-3).
     - The avatar path returns the last candidate after the maximum attempts, even when the vision gate REJECTED it. Post 100 shipped a rejected render (B-8).
     - `apply_contractions` runs **after** lint with no context check, producing "Do you've 15 minutes" (B-7).
     - Post 105 shipped with a slop HARD failure after being held, and no approval actor was recorded (B-3).
5. **Celery SUCCESS is being read as the outcome (G3, G5, G8).**
   - There were 0 FAILURE and 0 RETRY across 20,841 runs. The instrument itself works: it recorded 18 FAILUREs between 07-04 and 09-05 (C-3).
   - Tasks catch their own failures and return a string:
     - Appreciation returned "Appreciation DMs Sent" 44 of 44 times with 0 sent (L-3).
     - Newsletter reported SUCCESS on 2 of 2 failed publishes (A-6).
     - Follow-ups returned "Sent 5" when 2 landed, and 3 rows were written `success` for DMs the placeholder guard refused (L-1).
     - One confirmed invite has no `connection_requests` row (L-2).
   - Because nothing fails, the Celery-failure alert tile cannot fire. This is the CLAUDE.md invariant "success is the OUTCOME, never the click", broken at the task boundary.
6. **Sessions with images blocked blind the SDUI surfaces that were never exempted (G3). The link is confirmed; the mechanism is a hypothesis.**
   - `needs_images=True` (#1774, widened by #1778) exempts only the messaging and groups sessions. `newsletter.py` and `invites.py` never pass it, and the `Appreciation DMs` session opens without it (`outreach.py:1587`). All of this was read in code.
   - Every lane session on the three dead surfaces logs `images=blocked`:
     - Newsletter article editor: title, body and next all miss, and nothing has published since 08-25 (A-6/C-2).
     - Company-invite modal: it reads "Credits available 0/0" 15 of 15 times. Live-Validation sessions on the same days read 50/50 (A-5).
     - Recommendations-received: 0 found in 38 of 38 reads (A-9). #2065 is open.
   - Same shape as #1778, where an empty `<main>` turned out to be an images-blocked fastboot rather than a restriction.
7. **The lanes that grow the audience are starved of input (G1).**
   - The home feed passed 0 posts through its filters in 14 of 14 scans (A-7).
   - `scan_connection_candidates` found no candidates 14 of 14 times, and there have been 0 ledger rows since 09-07 (A-8).
   - Roster targets returned "No commentable on-topic post" 144 times, about 1 roster comment a day, so the roster-connect ladder has had 0 entries (A table). #2026 was closed for this.
   - Reciprocity has 1 `post_engagers` row ever, and no lead magnet has ever been sent (B-12).
   - Every comment now comes from group feeds, and that is exactly the path with the dedup defect in cause 2.
8. **Instruments count our own activity or disagree with each other (G8, G6).**
   - CQS `engagement_rate` still includes our own comments: post 108 scores 0.444 against 0 third-party engagement (C-5, B-13). #2023 fixed `post_stats` but not this reader.
   - The cost ledger (tier-priced) and `$ai_generation` (provider-priced) differ by about 13×, and MRR reads $79 in one place and $199 in another (C-6). This is the #752 defect class.
   - `follower_stats.connection_count` is pinned at 500, and `search_appearances` has been NULL since 08-28 (B-12).
   - The suppression tripwire has sat at `watch` for 9 days at a 75-91% impression drop and has never tripped. Its readings go to PostHog only (P1-C soft; A table).
9. **The plan and mix governor do not converge (G2, G1).**
   - The window's posts split 35/41/24 value/authority/promo, against a 70/20/10 target.
   - 1 of 7 generated posts matches its day type.
   - The Saturday slot is never laid, so the plan delivers 2 posts a week against `posts_per_week=3`. It has been stuck "over 30 days out" since 20 slots were dropped on 09-12.
   - 101 and 102 were posted outside `posting_days` (B-10).
   - Media drifts off message: deck 110 promises "exact 5 checks" but shows 4, deck 105 is a generic e-commerce deck on a governance post, and video 101 has no captions (B-9).
10. **Browser capacity per run does not fit the work (G5, G3).**
    - Group commenting ran out of time in 11 of 14 runs, covering 4-5 of 25 groups (A-11). #1719 was closed for this.
    - Nightly post-stats Chrome hit memcg OOM on 09-21, 09-22 and 09-23, and rows per night fell from 14 to 7, 4 and 5 (C-4).

### 7.2 Plan

#### Immediate owner actions (≤ 48 h, before any code lands)

| # | Action | Why now | Evidence |
|---|---|---|---|
| O-1 | **Delete the host crontab line `0 14 * * * /home/lem/recovery-probe/probe.sh`** (`crontab -e` as `lem`) and archive `/home/lem/recovery-probe/`. | It clears the 429 breaker every day at 14:00 UTC. That is a disabled safety control on the live account. | C-1 (P2 confirmed) |
| O-2 | **Decide post 110 before 2026-09-24 16:38 UTC.** Either reject it, or edit the caption to "4 checks" and add an artifact CTA. | The deck shows 4 of the "exact 5 checks" (gate 5, Monitoring, has no slide), and it is a promo post with no artifact CTA. | B-9, B-10; P2 "Post 110 approved, 16:38" |
| O-3 | **Hold newsletters 14-16 and all later editions** before 2026-09-29 13:00. Set `auto_publish_newsletters=0` (or return 14-16 to `draft`) until each is rewritten against the story bank. Do this **before** I-04 ships: fixing the editor would publish them. | NL14 claims "a real number from a real client". 6 of 7 editions carry invented client stories. | B-2, B-6 (P2 confirmed) |
| O-4 | **Do not approve the pending nurture DMs 40-43.** They contain the invented "reduced drift by about 30%" and the broken "you've to be a coder". | Once approved, they go straight to the send path, which has no content gate (cause 4). | B-2, B-7 (DB-3) |
| O-5 | **Decide whether to pause group-feed commenting** (a Redis manual pause) until I-01 phase 1 is deployed. | The same-post double comment is running now, at about 1.6 pairs a day. | A-1 |
| O-6 | **Check on-platform and delete** the second comment of each double pair that is still visible. Start with Kirill Pokidov (10 comments in 12 days) and the 6 comments the outcome sweep read as removed (1196/1255/1277/1309/1375). | The self-replies are the harmful artifacts, and some appear to have been removed already. Only the owner can judge and act on this. | A-1, A-2 |
| O-7 | **Reset the host checkout** that the crons use to `origin/main`, or better, pin it to the deployed tag (see I-22). | #2069 was not in effect for the 09-21 SDUI sweep. | C-12 |
| O-8 | When convenient, **re-auth YouTube** via `POST /admin/youtube-token` (#1094, open since 08-07). | invalid_grant was logged on 09-16 and 09-23. It is not urgent while tutorials are off. | C-3 |

#### 30 / 60 / 90 days

"Baseline" means this window's measured value. Each target has one verifier, and "measured by" names it. A V-* id refers to a recipe in Appendix A.

| Horizon | Goal | Outcome | Baseline → target | Measured by |
|---|---|---|---|---|
| **30 d** | G4 | Nothing but a successful login clears the breaker | a daily external wipe → 0 non-login clears | host `crontab -l` has no `clear_rate_limit` caller; the I-02 audit log line shows the caller on every clear |
| 30 d | G4 | No second comment on one post | 22 pairs in 14 d → **0** | V-PAIRS over 14 days of `/opt/lem/logs` after deploy |
| 30 d | G4 | Session density on post days | 41 peak per 30 min → **≤ 15** | V-DENS over 3 consecutive post days |
| 30 d | G2 | Invented first-person facts shipped (posts, newsletters, group posts, DMs, comments) | NL 6/7, comments 24/102 → **0 on newsletters, group posts and DMs; ≤ 5% of comments** | V-FACT plus V-FACT over each surface's shipped rows |
| 30 d | G2 | Held posts reach LinkedIn only with a recorded human approval | 2/2 held posts published (0 approvals recorded) → **0 unapproved** | SQL: posts that went PENDING→POSTED with no approval row |
| 30 d | G3 | Every lane task's Celery state reflects its outcome | 0 FAILURE in 20,841 runs → a failed publish, an appreciation run that sends 0, and a refused DM each record a non-SUCCESS or explicit `no_op` outcome | Flower `/api/tasks` state counts plus I-07 unit tests |
| **60 d** | G3 | Newsletter publishes again | 0 since 08-25 → **≥ 2 editions** with `published_url NOT NULL`, each owner-approved and story-bank clean | `newsletter_editions` SQL |
| 60 d | G3 | Company-page invites | 0 in 14 d → **> 0 a week, or a logged `unknown` reading (never `credits_exhausted` on 0/0)** | grep `Credits available` plus the lane ledger |
| 60 d | G3 | Comments addressable by URN | 30% → **≥ 80%** of third-party comments | `commented_posts` key prefix counts |
| 60 d | G3 | Comments removed on re-read | 6/20 (30%) → **≤ 10%** | `comment_outcomes` status counts |
| 60 d | G3 | Home feed and connection lanes have input | 0/14 home scans passing filters; 0 connection candidates → **≥ 1 candidate in ≥ 50% of runs** in each lane | the `Engagement scan:` funnel line; `connection_requests` rows |
| 60 d | G8 | Engagement numbers count third parties only | CQS 108 = 0.444 vs 0 → CQS equals `post_outcome.third_party_engagement_rate` | I-20 SQL cross-check |
| 60 d | G6 | Cost instruments agree | ~13× gap; 104/110 NULL `task_name` → **within 20%; < 5% NULL** | `cost_ledger` vs PostHog `$ai_generation` HogQL for the same day |
| 60 d | G5 | Nightly slow lane and post-stats | red for 24+ nights; 4-7 rows a night → **7 green nights; rows equal eligible posts** | `slow-tests.yml` runs; `post_stats` rows per night |
| **90 d** | G1 | Third-party ER | 0.51% baseline (6/1,177), window 0.74% on n=6 → **≥ 1.0% on ≥ 20 posts** | V-ER (third-party engagement = reactions + non-own comments + reposts) |
| 90 d | G1 | Followers | +38 in 14 d (4,279 → 4,317) → **≥ 4,500** | `follower_stats` |
| 90 d | G1 | Author replies to our comments | 0 of 14 checked → **≥ 3 a week** | `comment_outcomes.author_replied` |
| 90 d | G2 | Mix and cadence | 35/41/24, 1/7 day-type match, 2 posts a week → **within ±10 pp of 70/20/10, ≥ 80% day-type match, posts_per_week met** | V-MIX plus a mix SQL over 30 days |
| 90 d | G7 | Every merged fix verified live within 7 days | 17 of 49 PRs unverified → **0 unverified older than 7 d** | a release checklist in the audit follow-up |
| 90 d | G8 | Every reported number measures what it names | 5 wrong or blind instruments (C obs.) → 0 | re-run the §3 observability-truth table |

Sequencing: O-1 through O-4, then I-01, I-02, I-03 (critical). I-11 comes before any content fix, because a gate that the heal path bypasses fixes nothing. I-08 must land before I-04, and O-3 must be done before I-04 deploys.

### 7.3 Issue backlog

**How these are filed.**
- Every issue is filed through `.github/ISSUE_TEMPLATE/agent-task.yml`, with Context / Scope / Acceptance / Verifier / Phase.
- Milestones: **M30** is "Milestone 30: Audit 2026-09 — Safety & Broken Lanes" (S0/S1). **M31** is "Milestone 31: Audit 2026-09 — Quality & Growth" (S2/S3).
- `agent:ready` is set only when every acceptance box has a concrete verifier and no documented invariant or product decision is being decided.
- `risk:live-linkedin` is a merge sign-off label and can sit alongside `agent:ready` (precedent: #1774, #1778, #1625).
- Dedupe used `env -u GH_TOKEN gh issue list --state all --search …` on 2026-09-24. "Reopen-as-regression" means: reopen that issue and add this evidence, instead of filing a new one.

#### Index

| ID | Title | Sev | Priority | MS | Labels | Findings | Dedupe | Filed |
|---|---|---|---|---|---|---|---|---|
| I-01 | fix(feed): never comment twice on one post — dedup on post identity, not the body hash | S0 | priority:critical | M30 | bug, risk:live-linkedin, agent:ready (phase 1) | A-1, B-1, A-2 | **reopen-as-regression #580** (and #474, same class)  #580 (reopened) |
| I-02 | fix(ops): remove the host recovery-probe cron that clears the 429 breaker daily; audit every breaker clear | S0 | priority:critical | M30 | bug, ops, needs-human | C-1 | none found ("recovery probe clear_rate_limit", "breaker cleared cron": 0 hits)  #2092 |
| I-03 | fix(engagement): pre-post window re-dispatches every ~85 s — one claim per (user, post, lane) | S0 | priority:critical | M30 | bug, celery, risk:live-linkedin, agent:ready | A-3, A-13 | new; related #547, #696 (closed); temporal lead #2032/#2033  #2093 |
| I-04 | fix(newsletter): the article-editor session opens images-blocked — exempt it and re-ground the editor chain | S1 | priority:high | M30 | bug, sdui, risk:live-linkedin, needs-human (blocked on O-3, I-08) | A-6, B-6, C-2 | new; extends #1774/#1778; last grounding #804  #2094 |
| I-05 | fix(invites): company-page credits read 0/0 in images-blocked sessions and are treated as exhausted | S1 | priority:high | M30 | bug, sdui, risk:live-linkedin, agent:ready | A-5 | new (lane side); **comment on #2087** (probe side)  #2095 |
| I-06 | fix(outreach): the appreciation lane returns "Appreciation DMs Sent" with 0 sent | S1 | priority:high | M30 | bug, risk:live-linkedin, agent:ready | A-9, L-3 | new; **comment on #2065** (open SDUI drift); blocked by #2065 for the reader  #2096 |
| I-07 | fix(celery): lane tasks swallow failure — 0 FAILURE in 20,841 runs; the task state must reflect the outcome | S1 | priority:high | M30 | bug, celery, observability, agent:ready | C-3, A-9, L-1 | new; related #1813/#1816 (closed, lane-specific)  #2097 |
| I-08 | fix(content): enforce story-bank fact grounding HARD on newsletters and group posts | S1 | priority:high | M30 | bug, authenticity, risk:product-decision, needs-human | B-2, B-6 | new; posts half was #1971 (closed 09-11)  #2098 |
| I-09 | Comments still ship invented first-person metrics (24/102) | S1 | priority:high | M30 | bug, authenticity, needs-human | B-2, C-10, B-1 | **reopen-as-regression #1834**  #1834 (reopened) |
| I-10 | fix(dm): nurture and follow-up DMs have no content gate — a meeting ask and invented claims were sent | S1 | priority:high | M30 | bug, authenticity, risk:product-decision, needs-human | B-4, B-2 | new; related #618, #1969 (closed)  #2099 |
| I-11 | fix(content): the carousel heal auto-approves HELD posts without re-running the gates | S1 | priority:high | M30 | bug, agent:ready | B-3 | none found  #2100 |
| I-12 | fix(connections): the connection lane has been idle since 09-07 (0 candidates 14/14) and a confirmed invite is missing from the ledger | S1 | priority:high | M30 | bug, outbound, risk:live-linkedin, needs-human | A-8, L-2 | new; related #1813/#1814 (closed)  #2101 |
| I-13 | fix(feed): the home feed passes 0 posts through filters in 14/14 scans; roster is starved (144 no-commentable) | S1 | priority:high | M30 | bug, risk:product-decision, needs-human | A-7, A table (roster) | new for the home feed; roster half **reopen-as-regression #2026**  #2102 |
| I-14 | fix(dm): approved nurture drafts sent 3-9 days late in one burst — add a staleness cutoff and spacing within a run | S1 | priority:high | M30 | bug, agent:ready | A-4, A-10 | new; residual of #2078/#2079 (closed 09-16, merged after the burst)  #2103 |
| I-15 | fix(content): `apply_contractions` corrupts grammar ("Do you've 15 minutes") | S2 | priority:medium | M31 | bug, agent:ready | B-7 | none found  #2104 |
| I-16 | fix(image): the avatar path ships the last vision-REJECTED candidate | S2 | priority:medium | M31 | bug, risk:product-decision, needs-human | B-8 | new; related #1291, #1376 (closed)  #2105 |
| I-17 | fix(content): the deck/caption count check and topic fit are not gated (110: 5 vs 4; 105 off-topic) | S2 | priority:medium | M31 | bug, content-quality, agent:ready | B-9 | new; **comment on #1515** (decks) and **#1654** (video 101 captions NULL)  #2106 |
| I-18 | The content plan delivers 2 posts a week against posts_per_week=3 — Saturday never laid, stuck "over 30 days out" | S2 | priority:medium | M31 | bug, needs-human | B-10 | **reopen-as-regression #2021**  #2021 (reopened) |
| I-19 | fix(content): the mix governor misses 70/20/10 (35/41/24) and day-type archetypes (1/7) | S2 | priority:medium | M31 | bug, agent:ready | B-10 | new; related #618 (closed)  #2107 |
| I-20 | fix(metrics): CQS `engagement_rate` still counts our own comments | S2 | priority:medium | M31 | bug, observability, agent:ready | C-5, B-12/B-13 | new, follow-up of #2023 (closed 09-10; fixed post_stats only)  #2108 |
| I-21 | Cost ledger is tier-priced and diverges ~13× from `$ai_generation` | S2 | priority:medium | M31 | bug, observability, analytics | C-6 | **reopen-as-regression #752**  #752 (reopened) |
| I-22 | fix(ops): host crons run from a checkout 31 commits behind; perf_snapshot execs into nginx `web_app` | S2 | priority:medium | M31 | ops, needs-human | C-12, C-7 | new; #2086 pinned only the SDUI sweep  #2109 |
| I-23 | fix(errors): the error→issues cron skips a regression when the fingerprint's issue is CLOSED | S2 | priority:medium | M31 | bug, observability, agent:ready | C-8 | none found  #2110 |
| I-24 | Group commenting ran out of time in 11/14 runs (4-5 of 25 groups) | S2 | priority:medium | M31 | bug, celery | A-11 | **reopen-as-regression #1719**  #1719 (reopened) |
| I-25 | Mention card walk matched nothing (9×, 3 post days) | S2 | priority:medium | M31 | bug, sdui, risk:live-linkedin | A-12, C-8 | **reopen-as-regression #1985**  #1985 (reopened) |
| I-26 | fix(ci): the nightly slow lane has been red for 24+ nights — `test_baseline_matches_the_real_count` (2617 vs 0) | S2 | priority:medium | M31 | bug, ci, agent:ready | C-9 | none found  #2111 |
| I-27 | fix(post-stats): nightly scrape Chrome memcg OOM; rows per night fell from 14 to 4-7 | S2 | priority:medium | M31 | bug, celery, agent:ready | C-4, A §4a (tab crashed) | new; related #1751 (closed, 1×)  #2112 |
| I-28 | fix(comments): the owner-banned "hits home"/"game-changer" (13/102) and filler sentences (33/102) ship | S2 | priority:medium | M31 | bug, authenticity, agent:ready | B-11 | none found  #2113 |
| I-29 | fix(suppression): the tripwire compares young-post impressions against a mature baseline, and its readings are not logged | S3 | priority:low | M31 | bug, observability, agent:ready | C soft, B-12 | new; related #629, #1136 (closed)  #2114 |
| I-30 | fix(dm): follow-up ledger rows record `success` for refused DMs; near-duplicate "no worries" DMs on consecutive days | S2 | priority:medium | M31 | bug, agent:ready | L-1, B-5 | new; guard half was #2061 (closed)  #2115 |
| I-31 | fix(content): record the approval actor — held post 105 was published with slop HARD and no recorded approver | S2 | priority:medium | M31 | bug, risk:migration, agent:ready | B-3, B-13 | none found ("approval actor", "approved_by post": 0 relevant)  #2116 |
| I-32 | chore(db): `logs.action_type` has no invite / follow / group-comment type | S3 | priority:low | M31 | database, risk:migration, agent:ready | A-14 | none found  #2117 |
| I-33 | Nurture recipient names parsed as "to"/"on"/"liked"; "Hi Giri" to Saikumar | S3 | priority:low | M31 | bug | B-14 | **reopen-as-regression #1625**  #1625 (reopened) |

**Count: 33 issues. critical 3 · high 11 · medium 16 · low 3.** 7 of them were filed by reopening a closed issue as a regression: #580, #1834, #2021, #752, #1719, #1985 and #1625. I-13 was filed new as #2102 because its home-feed half is new; its roster half is cross-linked on #2026. 6 existing issues got an evidence comment instead of a new issue: #2087, #2065, #1515, #1654, #2026 and #1094 (the invalid_grant readings on 09-16 and 09-23).

#### Issue specs

Each spec supplies the form fields. The index row above supplies the title, labels and milestone. Phase is `single-phase` unless stated otherwise.

**I-01: fix(feed): never comment twice on one post (reopen #580)** · Phase: `phase 1 of 2`
- *Context:* 22 same-post pairs in 14 days, each 74-98 s apart, with the same author and age, and both logged as success (`cqc_lem_2026_09_12.log:583-586`; V-PAIRS: 22/65 feed-walk comments). The second comment grounds on our own first comment (1389→1390, 1422→1423). 63 of 90 comments are hash-keyed and have no permalink.
- *Scope:* Phase 1 (code only) has 3 parts:
  - A per-walk guard in `app/engagement/feed.py`: no second comment on the same author within one walk or 30 min.
  - Refuse to comment on a card whose text matches any of our own comments from the last 24 h.
  - A lead-in test for the re-read hypothesis.
  - Phase 2 (`risk:live-linkedin`) extracts the activity URN from group-feed cards, as a locator chain. Not in scope: the home-feed filters (I-13).
- *Acceptance:*
  - [ ] Unit: a walk that re-reads a card whose body is our just-posted comment yields no second comment.
  - [ ] Unit: two cards from the same author within one walk produce at most 1 comment.
  - [ ] After deploy, V-PAIRS over 7 days of logs reports 0 pairs.
- *Verifier:* `tests/unit/app/engagement/test_feed_*` (new cases); V-PAIRS over `/opt/lem/logs` 7 days after deploy.
- *Remaining phases:* phase 2 grounds URN extraction for group cards, with the target of ≥ 80% URN-keyed.

**I-02: fix(ops): remove the recovery-probe cron; audit breaker clears**
- *Context:* crontab has `0 14 * * * /home/lem/recovery-probe/probe.sh`. `probe.sh:25-28` calls `clear_rate_limit()` before its API call, which returns 401 every day, so the probe never disables itself (63 runs). The breaker is a safety control, and safety controls are not flags.
- *Scope:* Owner action: remove the line (O-1). Code: `clear_rate_limit()` in `utilities/linkedin/rate_limit.py` takes a required `reason` and logs INFO with its caller. Add a doc note in `docs/AUTOMATION_COOLDOWN.md` that host scripts must never call it. Not in scope: breaker semantics.
- *Acceptance:*
  - [ ] `crontab -l` for `lem` and `deploy` contains no `recovery-probe` entry.
  - [ ] Unit: `clear_rate_limit()` without a reason raises TypeError, and a call logs `reason=`.
  - [ ] Grep shows no caller of `clear_rate_limit` outside `src/`.
- *Verifier:* Box 1: the owner, via a Decision Comment. Box 2: `tests/unit/utilities/linkedin/test_rate_limit*.py`. Box 3: `grep -rn clear_rate_limit scripts/ /home/lem/*.sh` is empty.

**I-03: fix(engagement): pre-post window re-dispatch loop**
- *Context:*
  - Session peaks per 30 min were 41 (09-11), 38 (09-15), 22 (09-17) and 21 (09-22), against 9-15 on ordinary days (V-DENS).
  - Viewers were revisited 5-9 times in about 15 min, and 40 `engaged/failure` rows were written.
  - 32 passes ended "0 comment(s)".
  - The message "Daily comment budget spent (cap 20)" printed while the pacing draw was the real limit (`feed.py:2727-2733`).
- *Scope:* An idempotent Redis claim keyed on (user, post, lane), so the window runs each lane at most once per post. Profile-viewer engagement skips any viewer already touched today, before a session opens. Fix the budget message so it names the real limiter. Not in scope: the pacing engine itself.
- *Acceptance:*
  - [ ] Unit: a second dispatch within the window for the same post returns a `no_op` without opening a session.
  - [ ] Unit: the budget message names `pacing` when the pacing draw is exhausted.
  - [ ] Live: V-DENS shows ≤ 15 sessions per 30 min on the next 3 post days.
- *Verifier:* new cases in `tests/unit/app/engagement/`; V-DENS over the logs after deploy.

**I-04: fix(newsletter): images-blocked article editor**
- *Context:* ed11 (09-15) and ed13 (09-22) failed with "Selector miss: Article editor article_title/body/next". Both sessions were `images=blocked`, and `newsletter.py` never passes `needs_images` (code). Nothing has published since 08-25. The task reports Celery SUCCESS, and `log_error` at `newsletter.py:181,241` has no `exc=`.
- *Scope:*
  - Set `needs_images=True` on the publish session.
  - Rebuild the editor locators as a chain.
  - Pass `exc=` on both `log_error` calls.
  - Make the task's outcome non-SUCCESS on failure (shared contract with I-07).
  - **Blocked:** do not deploy until O-3 is done and I-08 has landed.
- *Acceptance:*
  - [ ] Unit: the publish session is requested with `needs_images=True`.
  - [ ] Unit: a selector miss produces a failed task state and an `$exception`.
  - [ ] Live: one owner-approved, story-bank-clean edition shows `published_url NOT NULL`.
- *Verifier:* unit lane; `linkedin-live-validation` probe of `newsletter_page`; `newsletter_editions` SQL.

**I-05: fix(invites): 0/0 credits read as exhausted**
- *Context:* The lane read "Credits available: 0/0" 15 of 15 times (all `images=blocked`), while Live-Validation read 50/50 on the same days (09-14 06:44 vs 16:31; 09-21 06:44 vs 15:59). The lane invited 0 in 14 days. `invites.py` has no `needs_images` (code).
- *Scope:* Treat a 0/0 reading as `unknown` (skip, with a WARNING naming the reading), never as `credits_exhausted`. Set `needs_images=True` on the lane session. The probe side stays in #2087.
- *Acceptance:*
  - [ ] Unit: `0/0` maps to `unknown`, and `0/50` maps to `exhausted`.
  - [ ] Unit: the session requests images.
  - [ ] Live: the lane logs a non-zero credit reading within 7 days.
- *Verifier:* `tests/unit/utilities/linkedin/test_company_page_inviter*.py`; grep `Credits available` after deploy.

**I-06: fix(outreach): appreciation false success**
- *Context:* "Appreciation DMs Sent" was returned 44 of 44 times. `appreciation_touches` has 0 rows. "Found 0 recommendation(s)" appeared 38 of 38 times. The session opens without images (`outreach.py:1587`).
- *Scope:* Return a structured result with the sent/found counts. Report `no_op` when 0 were sent. Pass `needs_images=True`. The recommendation reader is fixed under #2065.
- *Acceptance:*
  - [ ] Unit: a run with 0 found returns `sent=0` and never the text "Sent".
  - [ ] Unit: the session requests images.
- *Verifier:* unit lane; Flower result text over 7 days.

**I-07: fix(celery): the task state reflects the outcome**
- *Context:* 20,841 runs recorded 0 FAILURE and 0 RETRY. Before 09-05 there were 18 FAILUREs, so the instrument works. `engage_with_profile_viewer` and `auto_publish_edition` catch and return a string (code). The failure alert tile cannot fire.
- *Scope:* One shared outcome helper in `app/` that each lane returns: `landed` / `no_op` / `failed`. A `failed` outcome sets the Celery state to FAILURE. Apply it to the 6 tasks named in C-3. Extend the silent-success ratchet test (#1816).
- *Acceptance:*
  - [ ] Unit: each of the 6 tasks ends FAILURE on a simulated failed outcome.
  - [ ] Ratchet test: no lane task catches `Exception` and returns a string.
  - [ ] Flower shows ≥ 1 FAILURE within 7 days, or a documented `no_op` count.
- *Verifier:* `tests/unit/app/test_*`; the ratchet test; Flower `/api/tasks` state counts.

**I-08: fix(content): story-bank grounding HARD on newsletters and group posts**
- *Context:* The bank has 7 entries and no clients. NL11 and NL13-18 invent client audits. NL14 says "That's a real number from a real client". Group post 6 (published 09-22) says "I've seen a 40% drop in per-call cost". `auto_publish_newsletters=1`.
- *Scope:* Route both surfaces through the post-surface fact gate from #1971 in `content_alignment`. On a HARD failure, HOLD at PENDING. Severity per surface is set in `SURFACE_SEVERITIES`, **which is the product decision here.** Not in scope: comments (I-09) and DMs (I-10).
- *Acceptance:*
  - [ ] Unit: the NL14 sentence fixture is blocked on the newsletter surface.
  - [ ] Unit: the group post 6 fixture is blocked.
  - [ ] V-FACT over the editions generated in the next 30 days reports 0 invented client claims.
- *Verifier:* unit lane; V-FACT; the owner's Decision Comment on severity.

**I-09: reopen #1834: invented first-person metrics in comments**
- *Context:* 24 of 102 comments carry a first-person number ("cut allocation errors by about a quarter", "retries fall from 18% to 6%"). 6 of 25 sampled comments answer our own first comment, so the invented claim of comment 1 becomes the "post" that comment 2 grounds on. The quality contract passed those.
- *Scope:* Ground the fact gate on the **third-party post body plus the story bank only**. Our own prior comments never count as source text. Needs a human because the #1834 severity decision is being reopened.
- *Acceptance:*
  - [ ] Unit: a comment grounded only on our own prior comment fails the gate.
  - [ ] Regex over shipped comments for 14 days after deploy: ≤ 5% contain a first-person number.
- *Verifier:* unit lane; V-FACT over the `logs` comment rows.

**I-10: fix(dm): content gate for nurture and follow-up DMs**
- *Context:*
  - DM 37 was sent on 09-16 00:03. It asked "Do you've 15 minutes for a quick call" and claimed an invented DoD rollout.
  - DM 33 claimed an invented "cut iteration time about 30%".
  - `meeting_cta` has 0 hits, because the DM surface is not gated.
  - The owner's `business_goals` is "Earn 20-min diagnostic conversations", which **conflicts** with the documented meeting-ask ban. That conflict is the product decision.
- *Scope:* Apply `slop_lint`, the fact gate and the `meeting_cta` rule to the `dm` surface in `ai/dm_nurture.py` and `build_dm_from_template`. The owner decides whether a call ask is allowed in a warm, replied thread.
- *Acceptance:*
  - [ ] Unit: the DM 37 fixture is blocked (for fact, and for meeting if the owner keeps the ban).
  - [ ] Unit: a blocked DM stays `pending` and is never sent.
- *Verifier:* unit lane; the owner's Decision Comment.

**I-11: fix(content): the heal path bypasses HOLD**
- *Context:* `run_content_plan.py:2023-2027` sets APPROVED whenever the status is not POSTED and the slides are real. It also rewrites the body without re-gating. Post 108 went held (09-18 01:30) → "healed → approved" (03:16) → published.
- *Scope:* The heal may promote only `error` to `approved`, and only after the post-surface gates re-run on the new content. It must never promote `pending`. Clear `gate_reason` only when the gates pass.
- *Acceptance:*
  - [ ] Unit: a PENDING post stays PENDING after the heal.
  - [ ] Unit: an ERROR post whose new content fails a gate goes to PENDING, not APPROVED.
  - [ ] Unit: `gate_reason` is updated.
- *Verifier:* `tests/unit/app/test_run_content_plan*.py`.

**I-12: fix(connections): the lane is idle and a confirmed invite has no ledger row**
- *Context:* "No connection candidates found" 14 of 14 times. "No Connection Requests to Send" 4052 of 4052 times. There have been 0 rows since 09-07, even though `max_invites_per_day=10` and `auto_approve` are set. A confirmed invite on 09-22 (row 1399) has no `connection_requests` row.
- *Scope:* Find the first funnel stage that empties the candidate set (investigation output: a per-stage count). Write a ledger row for every invite path, including profile-viewer invites. The candidate thresholds may be a product decision.
- *Acceptance:*
  - [ ] A per-stage candidate funnel is logged on each scan.
  - [ ] Unit: the profile-viewer invite path writes a `connection_requests` row.
  - [ ] ≥ 1 candidate in ≥ 50% of scans within 14 days, or the owner signs off on a documented reason.
- *Verifier:* unit lane; grep of the funnel line; `connection_requests` SQL.

**I-13: fix(feed): the home feed passes 0; the roster is starved (reopen #2026 for the roster half)**
- *Context:* All 14 home scans (examining 10-28 posts each) passed 0 through the filters. `min_reactions=0`, `max_post_age_hours=48`, `fallback=False` 69 of 69 times. "No commentable on-topic post found for roster target" 144 times.
- *Scope:* A per-filter drop count for the home scan (which filter removes each post). The owner then decides the topic-gate threshold and whether the fallback applies. For the roster, re-check the #2026 fix against the 144 misses.
- *Acceptance:*
  - [ ] The scan line reports a count for each filter.
  - [ ] Unit: a fixture feed reproduces the drop in the stage it happens.
  - [ ] Owner decision recorded; after that, ≥ 1 home-feed comment a week.
- *Verifier:* grep of the `Engagement scan:` line; unit lane; the Decision Comment.

**I-14: fix(dm): stale nurture burst and intra-run spacing**
- *Context:* 5 drafts scheduled 09-07 to 09-13 were sent at 09-16 00:01-00:03, 3-9 days late, after a 7h20m requeue loop. Separately, 7 catch-up DMs went out in 4m50s on 09-21. #2079 (merged 09-16 08:50) fixed how the reaper ages DMs, but not staleness or spacing.
- *Scope:* A `send_scheduled_dm` older than N days past `scheduled_time` goes back to `pending` for re-approval. Add a minimum gap between DM sends within a run, drawn from `human_pacing`.
- *Acceptance:*
  - [ ] Unit: a DM 3 days past its slot is not sent and returns to `pending`.
  - [ ] Unit: consecutive sends in one run are separated by at least the drawn gap.
- *Verifier:* unit lane; `logs` dm timestamps over 14 days show no ≥ 3 DMs within 5 min.

**I-15: fix(content): `apply_contractions` grammar**
- *Context:* This reproduces locally: `apply_contractions('Do you have 15 minutes?')` returns "Do you've 15 minutes?". The bad forms shipped in DMs 1299 and 1303, follow-ups 1232/1233, and NL18 (4×).
- *Scope:* Contract "you have", "here is" and "it is" only when the phrase is not followed by a noun phrase or sentence-final. Otherwise leave it.
- *Acceptance:*
  - [ ] Unit: "Do you have 15 minutes", "you have to be" and "what it is." are unchanged.
  - [ ] Unit: "you have seen" becomes "you've seen".
- *Verifier:* `tests/unit/utilities/ai/test_content_alignment*.py`.

**I-16: fix(image): a rejected render ships**
- *Context:* The post 100 receipt says `gate_verdict: rejected`, and so does the first candidate. `image_gen.py:458-491` returns the last candidate. The documented invariant says the gate *fails OPEN*, which is meant for an unavailable gate, not a rejecting one.
- *Scope:* The owner decides between two outcomes on a rejected final: post with no image, or hold. The code distinguishes `gate_unavailable` (fail open) from `rejected`.
- *Acceptance:*
  - [ ] Unit: a final rejected verdict never reaches `posts.image_url`.
  - [ ] Unit: an unavailable gate still renders.
- *Verifier:* unit lane; the Decision Comment.

**I-17: fix(content): deck/caption and topic gates**
- *Context:* Deck 110 promises "exact 5 checks" and shows 4 (slides 2-5). Deck 105 is "AI in E-commerce: 5 Key Trends" on a governance post, with CJK text in a stock photo. Video 101 has NULL `caption_text`.
- *Scope:* A deterministic check that a number N in the caption matches the item slides in the deck. Add an off-topic deck check against the post thesis (reusing the `content_alignment` scorer). Captions go to #1654.
- *Acceptance:*
  - [ ] Unit: the 110 fixture (5 vs 4) HOLDs at PENDING.
  - [ ] Unit: the 105 fixture scores below the threshold.
- *Verifier:* unit lane.

**I-18: reopen #2021: plan stuck at 2 posts a week**
- *Context:* On 09-12 the log reads "dropped 20 planned slot(s)". Every day from 09-13 to 09-24 it reads "Start Date 2026-10-30 … over 30 days out, Skipped". The Saturday slot is never laid, even though `posting_days` includes 5. Posts 101 (Fri) and 102 (Mon) went out on days outside `posting_days`.
- *Acceptance:*
  - [ ] Unit: after a cadence change, the reconcile refills the next 30 days to `posts_per_week`.
  - [ ] Unit: no slot is laid outside `posting_days`.
- *Verifier:* unit lane; V-MIX over the next 30 days of the plan.

**I-19: fix(content): mix and day-type governor**
- *Context:* The window's posts split 35/41/24 against 70/20/10. 1 of 7 generated posts matched its day type. Promo posts 100 and 110 have no artifact CTA.
- *Acceptance:*
  - [ ] Unit: a 30-day plan is laid within ±10 pp of 70/20/10.
  - [ ] Unit: the archetype comes from `POST_DAY_TYPES`.
  - [ ] Unit: a promo without an artifact CTA HOLDs.
- *Verifier:* unit lane; mix SQL over 30 days.

**I-20: fix(metrics): CQS engagement counts self**
- *Context:* Post 108 has CQS `engagement_rate` 0.444 with 0 third-party engagement. `post_outcome.third_party_engagement_rate` exists but is not read.
- *Acceptance:*
  - [ ] Unit: CQS `engagement_rate` excludes `own_comments`.
  - [ ] SQL: CQS equals `third_party_engagement_rate` for posts 100-108.
- *Verifier:* unit lane; SQL cross-check.

**I-21: reopen #752: cost divergence**
- *Context:* The ledger records $4.97 of comment spend, while `$ai_generation` shows about $0.46 in total. `lem-medium` is served by ollama `gpt-oss:120b` at $0. 104 of 110 rows have a NULL `task_name`. MRR reads $79 in the alerts and $199 in the margin job.
- *Acceptance:*
  - [ ] Ledger is within 20% of `$ai_generation` for a sample day.
  - [ ] < 5% NULL `task_name`.
  - [ ] One MRR source.
- *Verifier:* HogQL vs SQL for the same day.

**I-22: fix(ops): stale host crons**
- *Context:* `perf_snapshot.sh` and `weekly_sdui_drift_check.sh` run from `/home/lem/linkedin_engagement_manager` at 31 commits behind. `perf_snapshot.sh` execs python in `web_app` (nginx), so margin is null on 48 of 63 lines. #2086 pinned only the SDUI sweep.
- *Scope:* Pin every host cron to the deployed tag (a fresh worktree per run, as #2086 does). Point `perf_snapshot` at `web_api_blue`/active. Inventory `crontab -l` for both users in `docs/`.
- *Acceptance:*
  - [ ] Every host cron resolves the deployed tag.
  - [ ] Margin is non-null on 7 consecutive `metrics.jsonl` lines.
  - [ ] The cron inventory doc exists.
- *Verifier:* owner check of `crontab -l`; `metrics.jsonl`.

**I-23: fix(errors): regressions on closed fingerprints**
- *Context:* Mention-card drift came back on 09-14 and 09-22 after #1985 closed, and group timeouts came back after #1719 closed. Neither was refiled.
- *Acceptance:*
  - [ ] Unit: a fingerprint whose issue is CLOSED and that recurs after the close date reopens the issue, or files one linked as a regression.
- *Verifier:* unit test of the cron script.

**I-24 (reopen #1719), I-25 (reopen #1985):** add the window evidence from A-11 and A-12 (11/14 runs, 4-5/25 groups, 2 tab crashes; 9× mention walk drift on 3 post days, 0 reply rows). The acceptance is each issue's own original acceptance, re-run against 14 days of logs.

**I-26: fix(ci): slow lane red**
- *Context:* `test_baseline_matches_the_real_count` fails nightly with 2617 vs 0, and has since at least 08-31.
- *Acceptance:*
  - [ ] The test passes locally with `-m slow`.
  - [ ] 7 green nightly runs.
- *Verifier:* `slow-tests.yml` history.

**I-27: fix(post-stats): OOM**
- *Context:* memcg OOM hit nodes 1/5/9 (1.5 GiB) at about 23:02 on 09-21, 09-22 and 09-23. "Browser tab crashed after 7/4/5 of 13/13/12". Rows per night fell from 14 to 7, 4 and 5. The once-daily warning never escalates.
- *Scope:* Recycle the driver every K posts, and resume from the last scraped post.
- *Acceptance:*
  - [ ] Unit: the scrape resumes after a tab crash.
  - [ ] Rows per night equal the eligible posts for 7 nights.
- *Verifier:* unit lane; `post_stats` SQL.

**I-28: fix(comments): owner-banned phrases and filler**
- *Context:* The owner's `comment_style` bans "hits home". 13 of 102 comments use "hit(s) home" or "game-changer", and 33 of 102 insert filler ("It works.", "That hurts."). The link to the humanizer's short-sentence rule (`content_alignment.py:1387`) is a **hypothesis**.
- *Acceptance:*
  - [ ] Unit: phrases banned in preferences are HARD on the comment surface.
  - [ ] ≤ 2% of comments over 14 days contain them.
- *Verifier:* unit lane; V-BANNED.

**I-29: suppression tripwire readings**
- *Context:* The tripwire has been `watch` for 9 days at a 75-91% impression drop and has never tripped. Mean impressions per post fell from 98 to 68. The readings go to PostHog only.
- *Lead resolution (gauntlet round 1):*
  - `_reach_signal` returns `watch` when SOME recent days drop and `tripped` only when ALL of them do. It behaved to its contract.
  - The 75-91% readings are an age artifact. Post 108 was read at 14 impressions when 1.5 days old, and post 102 at 25, both against a mature baseline median of 87.
  - There is no evidence of sustained suppression.
- *Scope:*
  - Compare posts only at equal age, or exclude posts younger than 72 h from `recent`.
  - Log each reading at INFO with the median, the recent values and the state.
  - Posting stays ungated either way (invariant).
- *Acceptance:*
  - [ ] Unit: a series whose recent post is 1 day old at 14 impressions against a median of 87 does not count that day as a drop.
  - [ ] A daily INFO log line carries the median, the recent values and the state.
- *Verifier:* `tests/unit/utilities/test_suppression*.py`; `grep "suppression" /opt/lem/logs/cqc_lem_*.log`.

**I-30: fix(dm): follow-up ledger truth**
- *Context:* Rows 1231, 1234 and 1237 are `success`, but the placeholder guard refused those sends (09-12 12:32-12:36). "Sent 5" was returned when 2 landed. 5 recipients got two near-identical "no worries" DMs on consecutive days.
- *Acceptance:*
  - [ ] Unit: a refused send writes `failure` with its reason.
  - [ ] Unit: the returned count equals the landed sends.
  - [ ] Unit: a second step within 48 h of a prior touch to the same recipient is skipped.
- *Verifier:* unit lane.

**I-31: record the approval actor**
- *Context:* Post 105 was held on 09-13 and published on 09-17 with slop HARD and authenticity 55. The `approvals` table has 0 rows for window posts, and there is no log line.
- *Scope:* A migration adding an `approved_by` column and a source to every status→APPROVED write (db-migration skill).
- *Acceptance:*
  - [ ] Every APPROVED write records its actor.
  - [ ] SQL shows no NULL actor on posts approved after deploy.
- *Verifier:* unit lane; SQL.

**I-32: `logs.action_type` enum**
- *Context:* There is no `invite`, `follow` or `group_comment` type, so those actions land as `engaged` or are not logged at all.
- *Acceptance:*
  - [ ] A timestamped migration adds the values.
  - [ ] The writers use them.
- *Verifier:* the db-migration checklist; unit lane.

**I-33: reopen #1625: recipient names**
- *Context:* DMs 30, 34 and 39 had the recipient recorded as "to", "on" and "liked", and one was addressed "Hi Giri" to Saikumar (lead 6). None was sent.
- *Acceptance:*
  - [ ] Unit: the name is taken from the profile header, never from notification text.
- *Verifier:* unit lane.

#### Watch list (reported only; below the issue threshold or from a single source)

- `follower_stats.connection_count` is pinned at 500, and `search_appearances` has been NULL since 08-28 (B-12, DB only).
- `golden_hour_report` counts our own seed as "comment found / reply sent" (C obs.).
- The PostHog key lacks `alert:read`/`dashboard:read`, so #2036 is unverifiable (C obs.; related #1453).
- Deploy holds: v0.173.13 took 9.5 h to reach prod (C-11).
- Post 100 was published 9h48m late via an orphan re-queue (B-15).
- `purge_post_assets` removes published images, so 5 of 6 published items cannot be re-audited. Related #1704/#1517.
- The group share box was not found on 09-15, 1 of 2 runs (related #1621).
- **33 Live-Validation sessions ran on the prod account**, 24 of them in about 2 h on 09-14. Off the Grid pool does not make them safe for the account. Watch this alongside I-03 session density.
- 2 stale-invite runs hit `budget_reached` with `withdrawn_today=0` (A table).
- A contact's first name appears in a WARNING line shipped to PostHog Logs (C security, minor).
- One carousel pydantic parse error on 09-18 (A §4a).

Not measured, and so not graded: PostHog flag values and the suppression readings; whether any DM "Sent" was confirmed in the thread; the pixels of the purged images 100 and 102; the prod `VIDEO_CAPTIONS_ENABLED` value; who approved post 105.


---

## 8. Gauntlet loop: verdict trail

The pieces were built by isolated builder subagents. Each was judged by a fresh critic instance that saw only two unlabeled, order-shuffled documents: the draft and a named in-repo exemplar. The cap was 3 rounds per piece.

| Piece | Round | Exemplar (blind label) | Critic | Winner | Single biggest gap named |
|---|---|---|---|---|---|
| 2 Artifact quality & media alignment | 1 | docs/content-quality-audits/image.md (A) vs draft (B) | fresh critic #1 | **Draft (B)** | No ranked, time-bound fix list (deck 110 today, NL14-16 un-approve); cites internal P1/P2 ledgers reader cannot inspect. Resolution: ranked actions live in §Plan (piece 3); evidence appendix added to final report. |
| 1 Scorecard, lists, failures, findings F1-F37 | 1 | docs/content-quality-audits/text.md (B) vs draft (A) | fresh critic #2 | **Draft (A)** | F31 suppression tripwire left unverified WATCH and unreconciled with F27 (75-91% drop vs 98->68 mean). Resolution by lead: read suppression.py:_reach_signal (origin/main) — `watch` = SOME of last 3 posting days drop >= ratio, `tripped` = ALL; the 75-91% readings come from immature per-post impressions (post 108 read at 14 impressions 1.5 days old, post 102 at 25) vs a matured baseline median 87; mature window posts sit at 67-160. Tripwire behaved per its contract; no sustained suppression evidence. F31 re-scoped to S3 "tripwire compares immature impressions -> noisy watch", reconciled with F27 (~30% mean drop, n=6). |
| 3 Root causes, plan, issue backlog | 1 | docs/engagement-growth-analysis-2026-07.md (A) vs draft (B) | fresh critic #3 | **Draft (B)** | Live verifiers were unversioned scratch scripts (pairs.py, dens.py, nlscan.py, …), so acceptance boxes were not reproducible by an agent. Resolution by lead: Appendix A replaces each with a self-contained grep/SQL recipe (V-PAIRS, V-DENS, V-FACT, …) and issue bodies cite recipe ids. |

**Outcome:** 3/3 pieces won their blind comparison in round 1. None parked `needs-human`. Each critic was a fresh instance that saw only two shuffled, unlabeled documents. Every named gap was closed by a targeted edit and not re-litigated. The lead also made two consistency edits: F28, F29 and F30 were promoted from WATCH to FILE so the findings match the backlog (I-17, I-28, I-26), and F31/I-29 were re-scoped after reading the tripwire code.


---

## Appendix A: Verifier recipes

The drafts name audit scratch scripts (`pairs.py`, `dens.py`, `nlscan.py`, `sample.py`, `a8.py`, `days.py`, `gram.py`). Those files do not ship with this report. Every acceptance box in the issue backlog uses the recipes below instead. Each recipe is self-contained and read-only, and runs on the VPS as `lem`.

- DB recipes run inside the prod worker, like this:
  `printf '%s\n' "from cqc_lem.utilities.db import get_db_connection" "c=get_db_connection();cur=c.cursor()" "cur.execute(\"\"\"<SQL>\"\"\");print(cur.fetchall())" | sudo docker exec -i celery_worker python -`
- In the table, `\|` stands for a literal pipe `|`. Unescape it before pasting a recipe into a shell.
- Always confirm the target with `SELECT @@hostname` before trusting a number. It must return `mysql_db`, never `lem-it-mysql`.

| Id | Measures | Recipe |
|---|---|---|
| **V-PAIRS** | Same-post double comments. Replaces `pairs.py` (I-01) | `grep -h "Commented on .*'s post (score" /opt/lem/logs/cqc_lem_2026_MM_*.log \| sed -E "s/^(\S+ \S+) Commented on (.*)'s post \(score [0-9.]+, age ([^)]*)\) \((.)\/(.)\).*/\1\|\2\|\3\|\4/" \| awk -F'\|' 'p==$2 && a==$3 && $4==2{n++} {p=$2;a=$3} END{print n+0}'`. Target is **0**. Audit value: 22 |
| **V-DENS** | Peak LinkedIn sessions in a 30-minute window. Replaces `dens.py` (I-03) | `grep -h "Selenium session '" /opt/lem/logs/cqc_lem_2026_MM_DD.log \| cut -c1-16 \| awk '{split($2,t,":"); b=t[1]*2+int(t[2]/30); c[$1" "b]++} END{for(k in c) if(c[k]>m)m=c[k]; print m}'`. This counts fixed 30-minute buckets, so it reads slightly lower than a sliding window. Target ≤ **15**. Audit peak (sliding): 41 |
| **V-HOMEFEED** | Home-feed comments. Covers A-7 and I-13 | `grep -h "Engagement scan" LOGS \| awk '{split($2,t,":"); h=t[1]+0} h<17 \|\| h>22' \| grep -o "feed [0-9]*)" \| awk '{s+=$2} END{print s+0}'`. Audit value: 0 over 15 scans |
| **V-URN** | Share of third-party comments addressable by URN (A-2, I-01 phase 2) | SQL `SELECT SUM(post_url LIKE 'feedurn://%%')/COUNT(*) FROM logs WHERE action_type='comment' AND result='success' AND post_id IS NULL AND created_at>=NOW()-INTERVAL 14 DAY`. Audit value: 0.30. Target ≥ 0.80 |
| **V-REMOVED** | Comments removed on re-read | SQL `SELECT status,skip_reason,COUNT(*),SUM(like_count),SUM(reply_count) FROM comment_outcomes WHERE created_at>=NOW()-INTERVAL 14 DAY GROUP BY 1,2`. Audit value: 6 removed of 20 read |
| **V-FACT** | Invented first-person numbers per surface. Replaces `nlscan.py` and `sample.py` (I-08, I-09, I-10) | SQL over `newsletter_editions.body`, `group_post_drafts.content`, `scheduled_dms.message` and `logs.message` (action_type comment/dm) with `REGEXP '(^\|[^a-z])(we\|our\|I\|my)[^.]{0,80}[0-9]+ ?%'` and `REGEXP 'client'`. Every hit is then checked by hand against `SELECT title,body FROM story_bank WHERE active=1`. Target: 0 on newsletters, group posts and DMs; ≤ 5% of comments |
| **V-GRAMMAR** | Contraction corruption (I-15) | SQL `... REGEXP "(Do you've\|you've to \|you've a \|here's like)"` over the same columns. Audit value: 2 sent DMs, 2 follow-ups, NL18 ×4 |
| **V-BANNED** | Owner-banned phrases (I-28) | SQL `SELECT COUNT(*) FROM logs WHERE action_type='comment' AND created_at>=NOW()-INTERVAL 14 DAY AND message REGEXP 'hits? home\|game.changer'`. Audit value: 13 of 102 |
| **V-ER** | Third-party engagement rate. Replaces `a8.py` (G1) | SQL, using the latest `post_stats` row per post: `SUM(reactions + GREATEST(comments-own_comments,0) + reposts)/SUM(impressions)`. Audit value: 3/408 = 0.74% (baseline 6/1,177 = 0.51%) |
| **V-MIX** | Content mix and day type. Replaces `days.py` (I-19) | SQL `SELECT content_mix,COUNT(*) FROM posts WHERE scheduled_time>=NOW()-INTERVAL 30 DAY GROUP BY 1`. Day type: `DAYOFWEEK(CONVERT_TZ(scheduled_time,'+00:00','America/New_York'))` checked against `POST_DAY_TYPES` in `utilities/ai/content_framework.py` |
| **V-HELD** | Held posts published without an approval | SQL `SELECT id,status,authenticity_score,gate_reason FROM posts WHERE gate_reason IS NOT NULL AND status='posted' AND updated_at>=NOW()-INTERVAL 30 DAY`, cross-checked against the `approvals` table. Audit value: 105 and 108 |
| **V-CELERY** | Task state reflects the outcome (I-07) | `sudo docker exec celery_flower wget -qO- 'http://localhost:5555/api/tasks?limit=100000&state=FAILURE' \| python3 -c 'import sys,json;print(len(json.load(sys.stdin)))'` (add Flower basic auth if it is enabled). Audit value: 0 of 20,841 |
| **V-CRON** | No host caller clears the breaker (I-02) | `crontab -l \| grep -c recovery-probe` must be 0, and `grep -rln clear_rate_limit /home/lem/*/ --include=*.sh` must be empty |
| **V-NEWS** | Newsletter delivers | SQL `SELECT id,status,published_url FROM newsletter_editions WHERE scheduled_for>=NOW()-INTERVAL 30 DAY` |
| **V-CREDITS** | Company-invite credit reads | `grep -h "Credits available" LOGS \| awk '{print $NF}' \| sort \| uniq -c`. Audit value: 15× `0/0` in the lane, 4× `50/50` in Live Validation |
| **V-SLOW** | Nightly slow lane | `env -u GH_TOKEN gh run list --workflow slow-tests.yml --limit 7` |

## Appendix B: Evidence trail

- **Prod reads.** DB container `mysql_db`, schema `linkedin_manager`, confirmed with `SELECT @@hostname`. Logs: `/opt/lem/logs/cqc_lem_2026_09_{10..24}.log`, 15,425 lines at INFO with UTC timestamps. Flower `/api/tasks` holds 26,137 tasks back to 09-06. Perf series: `/home/lem/perf-tracking/metrics.jsonl`. PostHog HogQL used a query-scoped key that was never printed. The PostHog MCP was unavailable (401). GitHub was read with `env -u GH_TOKEN gh`.
- **Not measured**, and therefore not graded:
  - Nothing was checked live on LinkedIn. The read-only probe was blocked by the session's permission classifier, so no comment was spot-checked on the platform.
  - PostHog flag values.
  - PostHog alert and dashboard state, because the key lacks `alert:read` and `dashboard:read`.
  - Whether any DM "Sent" landed in the thread.
  - Avatar likeness.
  - The pixels of the purged images for posts 100 and 102.
  - The prod value of `VIDEO_CAPTIONS_ENABLED`.
  - Who approved post 105.
  - DEBUG-level fail-open paths, since prod runs at INFO.
- **Read-only guarantee.** No DB, Redis or LinkedIn writes were made. The one on-platform probe was not run.
