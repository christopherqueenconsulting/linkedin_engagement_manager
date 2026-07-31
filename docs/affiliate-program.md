# Affiliate / Ambassador Program

**Issues:** #737 (status + consent + disclosure), #770 (the (B) writer) · **Status:** built, owner
decision applied (1A 2A 3A) · **Created:** 2026-07-27 · **Updated:** 2026-07-31

LEM's marketing arm is its own users. Every account joins the referral program by default, gets a
referral link, and earns extra free-trial time for people they bring in who actually get set up.
This is **permanent** — not a launch-phase promo — so it outlives P0/P1/P2.

Related: `docs/marketing-attribution.md` (#658, the `ref=` groundwork this reuses),
`docs/launch-and-marketing-plan.md` §A.2 (#499 extended trial), `docs/cost-performance-margin-plan.md`
(what a trial day costs).

---

## 1. The split that everything else hangs off

There are two completely different meanings of "affiliate by default", and they carry completely
different risk. They are built as two independent flags and are never read through one gate.

| | **(A) Affiliate STATUS** | **(B) Promotional CONTENT from the user's own account** |
|---|---|---|
| What it is | The user holds a referral link and earns trial time | LEM writes and publishes posts promoting LEM, **as the user**, on their LinkedIn |
| Default | **ON**, with a one-click opt-out | **OFF**, always |
| How it turns on | Automatically at signup | Only `POST /user/affiliate/promo-consent` with `consent_acknowledged`, which stamps a timestamp + consent version |
| Stored as | `affiliate_enrollments.status` | `affiliate_enrollments.promo_content_opt_in` + `promo_consent_at` + `promo_consent_version` |
| Gate | `affiliate.is_eligible()` / status | `affiliate.promo_content_allowed()` — the ONLY reader |

`promo_content_allowed()` requires the program on, the user still enrolled, the flag set **and** a
consent timestamp present. A flag with no timestamp is something other than the user saying yes, and
it is refused. There is deliberately no env var, no fleet default and no "enrolled implies consented"
shortcut: the account is the user's professional identity.

Opting out of (A) also withdraws (B) — consent to publish promotion cannot outlive the membership it
was given for.

The writer that turns (B) into real content is §2.1 below (issue #770). It reads the same
`promo_content_allowed()` and nothing else — there is still no env var, cohort or default that can
authorise promotion for a user who did not.

## 2. FTC compliance (16 CFR Part 255)

Extra trial time **is** compensation. An affiliate who promotes LEM therefore has a material
connection that must be disclosed **clearly and conspicuously in the promotional content itself**.

- **What counts as affiliate content:** content that publishes a LEM referral link — an owned-domain
  URL carrying `ref=` — or content LEM generated as (B) promo (`tagged=True`). The material
  connection comes from the link that earns the user trial time, not from the topic.
- **What counts as a disclosure:** the configured sentence (`AFFILIATE_DISCLOSURE_TEXT`), one of
  `#ad` / `#sponsored` / `#affiliate` / `#paidpartnership`, or an explicit compensated-relationship
  phrase. An author who reworded our sentence is not blocked; "partner" or "link" alone is not a
  disclosure.
- **Generation stamps it, publish enforces it.** `apply_disclosure()` appends the disclosure to
  affiliate copy at generation time — "it cannot be left to the user to remember". The publish gate
  in `post_to_linkedin` runs `disclosure_report()` and **flags the post `error`** if it is affiliate
  content without a disclosure. The post is never silently published undisclosed and never silently
  dropped: a human decides whether to add the disclosure or remove the link.
- **The seam that would otherwise defeat it:** #392 carries links out of the body into the first
  comment *before* the gate runs. The gate grades the body **and** the carried link — a first comment
  is still the same post's endorsement.
- **A deployment with `AFFILIATE_DISCLOSURE_TEXT` blank cannot publish affiliate content at all.**
  Blank means "no disclosure configured", never "no disclosure needed".
- The gate fails **open** on an unexpected error: this is a compliance check on a rare shape of post,
  not a new way for the whole posting path to break.

## 2.1 The (B) writer — `utilities/marketing/affiliate_content.py` (issue #770)

The ONE place promotional copy about LEM is produced. Four refusals define it:

**It is not a second consent path.** `promo_content_allowed()` is still the only authorisation.
`AFFILIATE_PROMO_CONTENT_ENABLED` can only turn the writer OFF — with it `True` and nobody
consented, nothing is written. An unreadable enrollment row reads as *no*.

**It is not an extra post.** An affiliate post **CLAIMS** one of the 70/20/10 governor's promo slots
(#618) instead of running beside it, so the 10% promo ceiling holds *by construction* rather than by
a second counter that could drift from the first. Only `content_mix == 'promo'` slots are eligible,
and only 1 in `AFFILIATE_PROMO_EVERY_N_PROMO_SLOTS` of those (default 3 ⇒ ≤1 post in 30). Every slot
it does not claim writes the author's own case study exactly as before. Carousels and videos are
never claimed — the copy is a short text post.

**It is not an author of facts.** The writer's allow-list is `PRODUCT_FACTS` and nothing else — the
same posture the story bank (#620) takes for personal specifics. It may not state a result, a
metric, hours saved or a testimonial, because LEM cannot verify any claim about what the tool did for
*this* user and an invented one is a false endorsement, not merely a bad post. The deterministic slop
lint (#625) runs on the draft with the same regenerate-then-block budget as everything else; copy
that cannot be made clean is **blocked**, never shipped.

**It is not silently published.** Two independent guarantees, deliberately not the same one twice:

| Layer | Guarantees |
|---|---|
| `apply_disclosure(..., tagged=True)` at generation | the disclosure **is written** |
| `affiliate_promo` quality gate (`quality_gates.py`) | the post is held **PENDING** for the author's explicit approval — it is the one gate that is not a quality verdict, and re-scoring cannot clear it. Consent to the program is a standing yes to LEM *writing* promotion; it is not a yes to any particular sentence going out over the author's name. Derived from the CONTENT (the referral link, scoped to the post's owner), so a post the user pasted their own link into is held too |
| `disclosure_report()` in `post_to_linkedin` | an undisclosed affiliate post **never publishes** |

**Pacing.** `human_pacing.ACTION_AFFILIATE_PROMO` draws its own daily budget from
`AFFILIATE_PROMO_MAX_PER_DAY`, so a rest day writes none. It is deliberately outside
`ENVELOPE_ACTIONS`: this is a generated POST, and posting is API-driven — it was never gated by the
engagement envelope. Pacing fails open; consent and the promo slot are the hard limits and neither
lives in Redis.

**Why a module of its own, given "no parallel per-content-type prompt helpers".** It draws voice,
mix directive, artifact-CTA policy, style and the slop lint from the shared core rather than
re-implementing them; what is local is the compliance shape (the fact allow-list, the verbatim link
and disclosure lines), which has legal teeth and belongs next to the rest of the program's policy.

**Surfaces.** Post only. A feed COMMENT promoting LEM under someone else's post, and a DM pitching
LEM to the author's connections, are both a different risk class and neither ships here.

## 3. Reward mechanics

Two rewards, deliberately different kinds — and the difference is what makes the opt-out honest
rather than a dark pattern:

| Reward | Trigger | Env | Revoked on opt-out? |
|---|---|---|---|
| **Enrollment bonus** | Holding affiliate status | `AFFILIATE_ENROLLMENT_BONUS_DAYS` (default **7**) | **Yes** — the user returns to the standard trial |
| **Referral bonus** | A referred user **ACTIVATES** | `AFFILIATE_REFERRAL_BONUS_DAYS` (default **14**) | **Never** — it was earned |
| Ceiling | Total granted days per user | `AFFILIATE_MAX_REWARD_DAYS` (default **90**) | — |

- A referral converts on **activation** (the #500 aha moment: a published post AND automated
  engagement), not on a click and not on a raw signup. A farm of dormant accounts pays nobody.
- The cap grants the **remainder** rather than refusing: at 85/90 days, a 14-day reward pays 5.
- The cap is measured on the ledger **SUM including revocations**, so opting out and back in is free,
  not profitable.
- A revoke never takes the trial below the user's own baseline: standard trial + any #499
  early-adopter grant + every referral day they earned.
- A paying subscriber is **not** paid in trial days (`not_on_trial`) — days they cannot spend would
  render as a granted reward that isn't one.
- A lapsed trial is extended from **today**, not from its past end date.

### 3.1 Why these numbers (owner decision: 1A 2A 3A)

These defaults were confirmed by the owner at merge. They remain overridable per environment, but the
production values are fixed below.

**What a trial day costs us.** A trial day is variable COGS (LLM + proxy + render) against **zero**
MRR. It is not lost revenue — a trialling user was not paying — but it is real spend and it delays
conversion. The margin plan targets ≥70% blended gross margin and ≥60% per-user CM at 100+ users, so
the reward has to be bounded rather than open-ended.

**What the market does.** SaaS referral programs cluster around *give one month, get one month* on
monthly plans (Dropbox/Evernote-style storage or time grants; Notion, Airtable and most PLG B2B tools
land in the same shape). Extra trial time is the cheapest currency available to a pre-revenue product:
it costs COGS, not cash, it needs no payout rail, and it has no tax/1099 exposure.

**Why 7 / 14 / 90.**
- **+7 enrollment** — a visible, immediate "you're in" that is half the standard 14-day trial, i.e.
  meaningful without materially changing anyone's evaluation window. Cheap enough to give everybody.
- **+14 per activated referral** — the industry "one free month"-shaped reward, at the same size as
  the standard trial, so it is legible ("another full trial") and it only pays for outcomes.
- **90-day ceiling** — roughly the point at which someone is using LEM indefinitely for free.
  Anyone who drove 6 activations is a real advocate; a conversation with them beats an unbounded grant.

**Alternatives the owner explicitly rejected** (kept here for reference; each is still a one-line env
change if policy changes later):
- *Enrollment-only* (`REFERRAL=0`): the owner's literal phrasing — affiliates simply get a longer
  trial. Simplest to explain; rewards nothing. Rejected in favor of outcome-based rewards.
- *Performance-only* (`ENROLLMENT=0`): nothing is given away by default, so nothing is revoked on
  opt-out, and the whole "reduced trial" framing disappears. Weakest enrollment hook. Rejected.
- *Bigger per-referral* (e.g. 30): stronger incentive, but 3 referrals ≈ a free quarter. Rejected
  because the 90-day cap is the intended guardrail, not the per-referral size.

## 4. Abuse guards

| Shape | Guard |
|---|---|
| Self-referral | `ref` == the new user's own id → stored `rejected/self_referral`, never converts |
| Duplicate attribution | `UNIQUE(referred_user_id)` — a replayed signup is a no-op |
| Referral from a member who left | Referrer not `enrolled` → `rejected/referrer_not_enrolled` |
| Unknown / forged `ref` | No such user → `rejected/unknown_referrer`; non-numeric `ref` is ignored entirely |
| Click farming | Reward pays on **activation**, never on a click or a raw signup |
| Unbounded grants | `AFFILIATE_MAX_REWARD_DAYS`, enforced on the ledger sum |
| Double payment | `UNIQUE(referral_id)` on the ledger; the enrollment bonus is held transactionally |

Rejections are **written**, not dropped: a fraud signal we can count is worth more than a row we
never wrote.

## 5. Surfaces

- **Data** — `affiliate_enrollments` / `affiliate_referrals` / `affiliate_rewards`
  (`V20260727191347__add_affiliate_program.sql`). Enums live in `db.py` (`AffiliateStatus`,
  `ReferralStatus`, `AffiliateRewardKind`).
- **Policy + orchestration** — `utilities/marketing/affiliate.py` (the ONE module).
- **(B) writer** — `utilities/marketing/affiliate_content.py`, claimed from
  `run_content_plan.create_content` on a promo slot; held by the `affiliate_promo` gate in
  `quality_gates.py` / `evaluate_post_gates`.
- **API** — `GET /user/affiliate`, `POST /user/affiliate/status`,
  `POST /user/affiliate/promo-consent`, `POST /user/affiliate/notice`.
- **Signup** — `api/main.py::_start_affiliate_membership` attributes the referral, then enrols.
  Best-effort throughout: the program is a perk and may never fail a signup.
- **Activation** — `utilities/onboarding.py::_convert_referral`, on the one-shot ACTIVATED transition.
- **Publish gate** — `app/run_automation.py::_affiliate_disclosure_gate`.
- **SPA** — `components/AffiliateNotice.tsx` (the enrollment notice, shown until acknowledged) and
  `pages/account/AffiliateCard.tsx` under Settings → Billing.

### 5.1 Notice + opt-out copy

Default enrollment is only fair if the user is told. The notice states what they are in, what they
get, how to leave — and that **nothing is posted from their LinkedIn account for this**. The opt-out
is one click, immediate, and the copy reads *"your trial returns to the standard N days"*, never
*"you will lose N days"*: the bonus is framed as a bonus on top, so leaving is a return to normal
rather than a punishment.

## 6. Analytics

`observability.track_affiliate_event` emits `affiliate_enrolled`, `affiliate_opted_out`,
`affiliate_promo_consent`, `affiliate_referral_attributed`, `affiliate_referral_rejected`,
`affiliate_referral_converted`, `affiliate_reward_granted`, `affiliate_reward_revoked`,
`affiliate_disclosure_blocked`, plus the (B) writer's own three: `affiliate_promo_generated`,
`affiliate_promo_published`, `affiliate_promo_blocked`. The SPA adds `referral_link_copied` and
`affiliate_notice_acknowledged`.

`affiliate_promo_blocked` (generation could not produce compliant copy) and
`affiliate_disclosure_blocked` (the publish gate caught content that arrived undisclosed) are
**never summed** — they are two different failures, and one piece of content can fire both.
Declining to write at all (no consent, not this slot, paced out) is deliberately NOT an event: a
refusal series that fires nine times out of ten tells nobody anything. `generated` vs `published` is
the read that matters — the gap between them is promotional copy the author chose not to approve.

These are **not** funnel events on purpose: the acquisition funnel is one ordered path per person,
and an affiliate event is about the **referrer**, not about the person moving through the funnel.
Emitting them there would put one person's referral conversions inside another person's journey.
Inbound referral traffic is already visible on the #658 **LEM Channels** dashboard via the
`referral` channel.

## 7. Configuration

| Env | Default | Meaning |
|---|---|---|
| `AFFILIATE_PROGRAM_ENABLED` | `True` | Master switch. Off = no links minted, no rewards, no gate. |
| `AFFILIATE_DEFAULT_ENROLLED` | `True` | Whether new users start enrolled in (A). |
| `AFFILIATE_ENROLLMENT_BONUS_DAYS` | `7` | Trial days for holding status (revoked on opt-out). |
| `AFFILIATE_REFERRAL_BONUS_DAYS` | `14` | Trial days per **activated** referral (never revoked). |
| `AFFILIATE_MAX_REWARD_DAYS` | `90` | Per-user ceiling on total granted days. |
| `AFFILIATE_REQUIRE_COMPANY_PAGE` | `False` | Restrict eligibility to accounts with a company page. Evaluated live. |
| `AFFILIATE_DISCLOSURE_TEXT` | `#ad — I get free …` | The FTC disclosure. **Blank blocks affiliate content entirely.** |
| `AFFILIATE_PROMO_CONSENT_VERSION` | `v1` | Bump when the (B) consent copy materially changes. |
| `AFFILIATE_PROMO_CONTENT_ENABLED` | `True` | Kill switch for the (B) **writer**. Cannot turn (B) on for anybody. |
| `AFFILIATE_PROMO_EVERY_N_PROMO_SLOTS` | `3` | 1-in-N promo slots may be claimed by an affiliate post. |
| `AFFILIATE_PROMO_MAX_PER_DAY` | `1` | Per-day ceiling on generated promotional pieces, drawn through human pacing. |
| `AFFILIATE_PROMO_PRODUCT_NAME` | `LinkedIn Engagement Manager` | What the copy may call the product. |

`BRAND_SIGNUP_URL` must be set for referral links to exist at all; with it unset, `referral_url` is
empty and the SPA says so rather than showing a broken link.
