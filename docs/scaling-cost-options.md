# Selenium Hosting — Cost & Feasibility Comparison (Hosted/Cloud vs Self-Managed)

**Status:** Research spike, no infra changes · **Issue:** #633 (spike; risk: product-decision) ·
**Date:** 2026-07-27 · **Owner:** Chris

> Companion docs: `docs/scaling-plan.md` (the compute/concurrency plan this spike widens),
> `docs/SELENIUM_GRID.md` (the self-managed Grid that's already **built, not enabled** — §5 there
> parked the "16 vCPU/64 GB vs second box" decision pending this doc), `docs/EGRESS_AT_SCALE.md`
> (why residential egress + ToS posture are load-bearing requirements below, not nice-to-haves).

## Decision (owner, 2026-07-27, PR #694)

**LEM stays self-managed. The hosted/cloud grid market is ruled out — all of it.** That means AWS
(Fargate Grid nodes, EC2 Grid nodes, Device Farm) **and all eight commercial vendors surveyed
below**, including Steel.dev and Browserbase, which this spike had flagged as the two structural
fits worth one more look. No vendor spike is planned and none is scheduled at any user tier.

Everything below is retained as the **evidence for that decision**, not as a shortlist. Two things
follow from that framing and matter when reading it:

- The per-vendor verdicts are what the research found, not standing recommendations. Where a vendor
  is described as a "fit" or "worth a spike", read that as *what the numbers said before the call
  was made* — the call went the other way.
- Prices and terms here were accessed 2026-07-27 and this market moves fast. Nothing in this doc
  should be treated as a live quote if the question is ever reopened; it would need re-researching
  from scratch.

The self-managed path itself is settled the same day: **Option B — a second same-provider Hostinger
box running Grid nodes (`docker-compose.grid-node.yml`, already built) — is the default horizontal
scale-out.** Option A ("upgrade to 16 vCPU / 64 GB") is retired, because the in-place resize it
assumed does not exist on the current provider (§4, §6).

## TL;DR

- **The premise that self-managed Option A ("upgrade to 16 vCPU / 64 GB") is a cheap in-place
  resize is wrong.** Hostinger's VPS line **tops out at KVM8 (8 vCPU / 32 GB)** — there is no
  bigger tier to resize into. A 16 vCPU/64 GB box is real money (~$315/mo Hetzner, ~$504/mo
  DigitalOcean) **and a full cross-provider migration** (DNS, data, Docker stack, Cloudflare
  Tunnel cutover), not a plan change. This flips `SELENIUM_GRID.md` §5's "A is cheaper if the
  choice were made today" call — see §6 below.
- **A second same-provider Hostinger box (Option B) is still the cheapest, lowest-risk, lowest-effort
  path** — ~$26–50/mo, same panel, no new vendor relationship, no ToS exposure, and it's exactly
  what `docker-compose.grid-node.yml` already targets.
- **Almost all of the hosted/cloud market is disqualified for LEM's specific use case** — a
  logged-in, cookie-persisted, days-to-weeks LinkedIn session over a per-user residential proxy —
  not because of price, but structurally:
  - **AWS Device Farm**: hard 40-minute session cap, `--user-data-dir` explicitly blocked. Cannot
    hold a login. Disqualified outright, independent of price.
  - **QA-testing clouds** (BrowserStack, Sauce Labs, LambdaTest, TestingBot): built for short,
    ephemeral, own-app test runs. **Sauce Labs' Acceptable Use Policy explicitly bans "any social
    media site other than for purposes of software testing"** — a direct textual conflict with
    LEM's use case, not an inference. The others aren't as explicit but no vendor confirms
    cross-session cookie persistence, and none is a clean fit.
  - **Browserless**: disqualified on protocol — CDP/Puppeteer-first, not native Selenium
    WebDriver, so it fails the "zero app-code-change" bar `get_docker_driver()` was built around.
  - **ZenRows / ScrapingBee**: structural mismatch — per-request scraping APIs, not persistent
    browser sessions. ZenRows' closest product caps session TTL at 1–15 minutes.
- **Two vendors were the only genuine fit on paper — Steel.dev and Browserbase — and they are ruled
  out too, by decision rather than by disqualification.** Both are built for exactly LEM's pattern
  (persistent authenticated sessions via a `Profiles`/`Contexts` API, BYOP residential proxy — free
  on Steel, custom extension loading, no session-length ceiling that matters at LEM's scale) and
  both price below every AWS option — Steel.dev ~$33–430/mo and Browserbase ~$47–399/mo across the
  10→100 user range, vs. AWS Fargate's ~$165–1,115/mo. Each also carried an unresolved blocker:
  Steel's actual ToS text (redirects to a Google Doc this research pass couldn't extract), and
  whether either vendor's WebDriver path is a true URL-repoint into `get_docker_driver()` or needs
  a connection wrapper (Browserbase confirmed needs one; Steel's Selenium support is documented but
  CDP-first). The owner's call (2026-07-27) was to not spend engineering time resolving either —
  self-managed only.
- **AWS Fargate/EC2 Grid nodes are real and technically workable** (residential-proxy egress layers
  on cleanly; on-demand session-length is unbounded) **but cost more than every self-managed and
  Steel/Browserbase option at every tier**, and add real new ops surface (NAT/ASG/ECS) LEM doesn't
  otherwise need. Fargate Spot / EC2 Spot are not recommended as primary capacity — a 2-minute-warning
  reclaim mid-comment/mid-DM risks a half-submitted action on a live account and the kind of session
  churn that trips LinkedIn's own anti-bot detection.
- **Decision (detail in §6, decision trigger in `docs/scaling-plan.md` §5f): self-managed only.**
  The default horizontal scale-out is Option B — additional same-provider Hostinger boxes running
  the Grid that's already built. It's cheap, proven, and carries zero new ToS/vendor risk. Nothing
  in the hosted/cloud market is pursued: not AWS Grid nodes, not Device Farm, not the QA-testing
  clouds (BrowserStack/Sauce Labs/LambdaTest/TestingBot/Browserless/ZenRows/ScrapingBee), and not
  Steel.dev or Browserbase. The first group is disqualified on its own merits; the last two are a
  deliberate owner call to keep the stack on infrastructure LEM already owns and operates.

---

## 1. What this is keyed to

Per `docs/SELENIUM_GRID.md` §4, the load test (`selenium_load_test.py`, issue #556) measured how
many concurrent Chrome sessions keep 95% of engagement work inside its window, on today's topology
(8 slots, lanes 3/2/2/1):

| Users | Sessions needed (measured, pre-stagger) | Staggered, **predicted** (what the pricing below used) | Staggered, **measured** (#634, 2026-07-27) |
|---|---|---|---|
| 10 | 5 | 4 | 5 |
| 50 | 14 | 11 | **15** |
| 100 | 27 | 20 | **28** |

Every cost column below is priced at **both** of the first two numbers as `staggered (measured)` —
e.g. `4 (5)` — because when this spike was written the staggered curve was still a prediction
pending #634, and pricing the conservative (pre-stagger measured) number avoided under-provisioning
if #634 came back worse than modeled. **It did:** #634 landed at 5 / 15 / 28, i.e. at or just above
the conservative column, so **read the parenthesised (measured) figure in every table below as the
live one** — the optimistic figure never materialised. One session more at 50 and 100 users moves
no verdict here: it is within Option B's next box, and it makes the hosted per-session vendors
slightly *more* expensive, not less. Ratio used
throughout: **~1 vCPU + ~1.2–1.5 GB RAM per concurrent Chrome session** (matches §4's measured 8
slots ≈ 6.5–8 vCPU / 6–8 GB), and **~65–70 active Selenium-minutes/user/day** (§4's per-user
workload total) for any vendor billed by browser-minute rather than by concurrency tier.

All prices below accessed **2026-07-27**; this market moves fast — re-verify before committing
money, per the issue's own instruction.

---

## 2. Cost comparison chart

### 2a. Self-managed baselines

| Option | $/mo @ 10 users | $/mo @ 50 users | $/mo @ 100 users | Setup/ops effort | Residential egress | ToS risk | Session-length fit | Rollback difficulty | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| **A. Upgrade primary box to ~16 vCPU/64 GB** | n/a — plan doesn't exist on current provider | ~$315/mo (Hetzner CCX43) or ~$504/mo (DO 16vCPU/64GB) | same box, at its ceiling (~50–60 staggered users per `SELENIUM_GRID.md` §5) | **High** — Hostinger has no such tier, so this is a **full cross-provider migration** (DNS, MySQL/Redis/assets data, Docker stack, Cloudflare Tunnel), not a resize | Unchanged — per-user proxies do the egress, independent of host | None (self-managed) | Unlimited (own box) | Migration is itself hard to reverse quickly (a second cutover) | **Not a resize anymore — corrects `SELENIUM_GRID.md` §5's premise. Only worth it if the app tier also needs headroom the second-box path doesn't give.** |
| **B. Second VPS running Grid nodes (same provider)** | ~$0 marginal (10 users' 4–5 sessions fit the existing 8-slot box) | ~$52–100/mo (2 Hostinger KVM8 boxes, 16 slots) | ~$78–200/mo (3–4 boxes, 24–32 slots) | **Low** — same panel, same provider, `docker-compose.grid-node.yml` already built for this exact topology (issue #556) | Unchanged | None (self-managed) | Unlimited | **Lowest** — one flag reverts the Grid overlay to `standalone` (`docs/SELENIUM_GRID.md` §2) | **Cheapest, lowest-risk, already built. Default horizontal path.** |

Hostinger KVM8 (the current box's own tier) prices **$25.99–29.99/mo promo, ~$49.99/mo at
renewal** — [hostinger.com/vps-hosting](https://www.hostinger.com/vps-hosting), accessed
2026-07-27. Hostinger's lineup has **no tier above KVM8** — confirmed by searching its full VPS
plan list; nothing between KVM8 and an unlisted, unpriced dedicated-server quote. Hetzner's
CCX43 (16 vCPU/64 GB) and CCX33 (8 vCPU/32 GB, the Option-B-equivalent second box on Hetzner) both
reflect Hetzner's **June 15, 2026 price increase** (CCX line +121–122%, DRAM-cost-driven,
new-orders/resizes only — existing servers keep old pricing) —
[northflank.com/blog/hetzner-cloud-server-price-increases](https://northflank.com/blog/hetzner-cloud-server-price-increases),
corroborated by
[wz-it.com](https://wz-it.com/en/blog/hetzner-price-increase-june-2026-cpx-ccx-alternatives/),
accessed 2026-07-27. DigitalOcean 16vCPU/64GB General Purpose droplet: **$504/mo**
($0.75/hr) — [digitalocean.com/pricing/droplets](https://www.digitalocean.com/pricing/droplets),
accessed 2026-07-27.

**Migration/ops effort, qualitatively:** an in-place Hostinger resize would be the cheapest ops
path *if it existed* (Hostinger's own docs describe resizes elsewhere in their lineup as
~10 minutes of panel work). It doesn't exist at this spec, so Option A is now a real migration —
DNS cutover, full data migration, Docker stack + secrets re-provisioning, Cloudflare Tunnel
re-pointing, a cutover window with real downtime risk absent a staged blue-green cutover. A
second box on the **same** provider (Option B) needs no such project — Hetzner additionally
offers native, unmetered private networking (VPC) between same-region boxes, which the mixed
Hostinger-primary + Hetzner-second-box combination would not get (that pairing needs WireGuard
instead).

### 2b. AWS-hosted Grid nodes

| Option | $/mo @ 10 users (4 or 5 sessions) | $/mo @ 50 users (11 or 14) | $/mo @ 100 users (20 or 27) | Setup/ops effort | Residential egress | ToS risk | Session-length fit | Rollback difficulty | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| **ECS/Fargate Grid nodes (on-demand)** | $165–206/mo | $454–578/mo | $826–1,115/mo | **Medium** — real, documented community pattern (not first-party AWS), needs a hub + service discovery + autoscaler on top of the container images LEM already runs | Yes — proxy-auth extension runs inside the Chrome container, host-independent; watch NAT Gateway ($0.045/hr + $0.045/GB) if nodes sit in a private subnet | None found specific to this pattern | Unlimited on-demand; **Spot is not viable** (2-min reclaim kills mid-session login state and risks a half-submitted LinkedIn action) | Low-medium — same Docker images, just repoint the hub URL | **Workable but pricier than self-managed at every tier, plus new ops surface (NAT/ASG/ECS)** |
| **EC2 Grid nodes (on-demand, c5.2xlarge, ~7 sessions/instance)** | $248/mo (1 instance) | $496/mo (2 instances) | $745–993/mo (3–4 instances) | **High** — you own AMI/patching, ASG, health checks; effectively a second, cloud-hosted twin of the existing infra | Same as Fargate | None found | Best AWS fit — unbounded on-demand session length; Spot viable only as a burst/overflow tier with cookies persisted externally | Low-medium | **Cheapest AWS option but most AWS ops burden; still costs more than Option B at every tier** |
| **AWS Device Farm (Desktop Browser Testing)** | disqualified | disqualified | disqualified | N/A | Unsupported path (AWS datacenter IPs; loading LEM's proxy-auth extension is explicitly "not officially supported") | Not confirmed as a ToS ban, but moot | **Hard 40-min session cap; `--user-data-dir` explicitly blocked** — cannot hold a login at all | N/A | **DISQUALIFIED — cannot hold a session past 40 minutes or persist a login, at any price** |

Fargate on-demand: **$0.04048/vCPU-hr + $0.004445/GB-hr**
([aws.amazon.com/fargate/pricing](https://aws.amazon.com/fargate/pricing/)); EC2 c5.2xlarge
on-demand: **$0.34/hr**
([economize.cloud/resources/aws/pricing/ec2/c5.2xlarge](https://www.economize.cloud/resources/aws/pricing/ec2/c5.2xlarge/));
Device Farm: **$0.005/browser-instance-minute**, session caps confirmed directly from
[docs.aws.amazon.com/devicefarm/latest/testgrid/techref-support.html](https://docs.aws.amazon.com/devicefarm/latest/testgrid/techref-support.html).
All accessed 2026-07-27; NAT Gateway and EC2/Fargate Spot figures are flagged in the source
research as **approximate** (Spot pricing renders client-side and moves in real time — pull a
live quote before budgeting off it).

### 2c. Commercial browser-automation / QA-cloud vendors

| Vendor | $/mo @ 10 users | $/mo @ 50 users | $/mo @ 100 users | Native Selenium? | Residential egress | ToS risk | Session-length fit | Custom extension | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| **BrowserStack Automate** | ~$325–400 (unverified extrapolation — multi-parallel pricing is sales-gated) | ~$1,100–1,350 | ~$2,100–2,600 | Yes | **No** — datacenter only; "Local Testing" is an inbound tunnel, not outbound-through-your-proxy | Product framed around testing *your own* app; no confirmed cross-session cookie persistence for Automate | 2 hr cap, 90s idle default | Yes (`.crx` load) | **Disqualified** — no residential egress, no confirmed login persistence |
| **Sauce Labs** | ~$900–1,500 (unverified) | ~$2,500–4,000 | ~$5,000–8,000 | Yes | **No** — same inbound-tunnel model | **Explicit AUP ban**: "any social media site other than for purposes of software testing," plus a ban on unattended/not-logged-in processes | 3 hr cap | Yes | **Disqualified — direct, written ToS conflict, not an inference** |
| **LambdaTest** | ~$580–750 (unverified, 5-parallel estimate; 14/27 sales-gated) | unpublished | unpublished | Yes | Unconfirmed `LT_PROXY_HOST` capability — may route outbound traffic through an external proxy, not verified live | No explicit ban found | 30 min auto-cap | Yes | **Weak candidate — not disqualified in writing, but no confirmed login persistence and unpublished pricing at scale; needs a live spike before serious evaluation** |
| **TestingBot** | ~$90/mo flat (Automated Pro, nominally unlimited minutes/concurrency) | ~$90/mo flat (same caveat) | ~$90/mo flat (same caveat) | Yes | Unknown — no proxy/egress info published | No explicit ban found | Not published | Yes (Playwright-documented; presumed same for Selenium) | **Thinnest documentation of any vendor surveyed — cannot recommend without a direct vendor conversation on real limits behind "unlimited"** |
| **Browserless** | ~$140/mo (Starter) | ~$350/mo + likely overage (Scale) | ~$350/mo + overage, or Enterprise | **No — CDP/Puppeteer/BQL-first** | Yes, paid (6 units/MB residential) + self-hosted BYOP | No explicit ban found | 15–60 min by plan | Yes | **Disqualified on protocol — not native Selenium WebDriver, fails the zero-app-code-change requirement** |
| **Steel.dev** | ~$33–35/mo | ~$280–290/mo | ~$410–430/mo | Partial — Selenium documented, but CDP-first; may need a connection wrapper | **Yes** — managed residential proxies, Dedicated IPs, **free BYOP on all plans including free tier** | **Not independently verified** — real ToS is a Google Doc this research pass couldn't extract | 15 min–24 hr by plan; **explicit Profiles API persists cookies/login/extensions across separate sessions** | Yes (persists via Profiles) | **Strongest cost fit found, but NOT PURSUED** — its Selenium-compatibility check and real ToS read were never run; ruled out with the rest of the hosted market (2026-07-27) |
| **Browserbase** | ~$47–50/mo | ~$203–218/mo | ~$374–399/mo | Yes, but needs a **custom `RemoteConnection` wrapper** — not a pure `SELENIUM_HUB_HOST` repoint | **Yes** — residential by default (201 countries, geo-targeting) + BYOP | No explicit ban found; vendor markets a "Social Media Scraper" use case as intended use | 6 hr default cap; **explicit Contexts API persists cookies (incl. session cookies) across separate sessions** | Yes | **Strongest overall fit, but NOT PURSUED** — mature docs, no ToS red flag; the Selenium wrapper and per-user IP stability across days were never verified; ruled out with the rest of the hosted market (2026-07-27) |
| **ZenRows / ScrapingBee** | not modeled — different billing unit (per-request, not per-minute/concurrency) | not modeled | not modeled | No | Bundled but moot | Neither explicitly names LinkedIn; ScrapingBee's AUP explicitly puts LinkedIn's own ToS risk back on the customer | **1–15 min hard cap (ZenRows Scraping Browser); no session concept at all (ScrapingBee)** | N/A | **Disqualified — structural mismatch, not a policy one. Cannot hold a login for more than 15 minutes at best.** |

Full per-vendor pricing tiers, quotes, and source URLs are in the research notes this table was
built from (BrowserStack, Sauce Labs, LambdaTest, TestingBot: browserstack.com/support/faq,
saucelabs.com/doc/acceptable-use-policy, testmuai.com/pricing, testingbot.com/pricing; Browserless:
browserless.io/pricing; Steel.dev: docs.steel.dev/overview/pricinglimits,
docs.steel.dev/overview/profiles-api; Browserbase: docs.browserbase.com/platform/browser/
core-features/contexts, browserbase.com/pricing; ZenRows/ScrapingBee: docs.zenrows.com/
first-steps/pricing, scrapingbee.com/acceptable-use-policy — all accessed 2026-07-27).

---

## 3. Deal-breaker checklist (from the issue), scored

| Check | AWS Fargate/EC2 | AWS Device Farm | BrowserStack/Sauce/LambdaTest/TestingBot | Browserless | Steel.dev / Browserbase | ZenRows/ScrapingBee | Self-managed (A/B) |
|---|---|---|---|---|---|---|---|
| Datacenter-only egress (no residential path) | Layerable (proxy inside container) | Yes, unsupported to change | Yes — no outbound-through-proxy mechanism found | Layerable (paid or BYOP) | **No — residential/BYOP native** | Bundled but moot | N/A — own box, own proxy |
| Per-minute billing vs. multi-minute logged-in sessions | On-demand: fine. Spot: mismatched | Mismatched (40 min cap) | Mismatched (30 min–3 hr caps, no persistence) | Mismatched (15–60 min caps) | **Fits** (hour/day-scale, persisted) | Mismatched (1–15 min cap) | N/A |
| ToS forbids social-automation/scraping | Not found | Not confirmed, moot (technical disqualifier) | **Sauce Labs: explicit ban.** Others: not found in writing | Not found | Steel: **never verified** (couldn't read real ToS; not pursued). Browserbase: not found, positive signal (markets this use case) | Not found, but customer bears the risk explicitly | N/A |
| Can load the MV3 proxy-auth extension | Yes (own container) | Unsupported | Yes (BrowserStack/TestingBot confirmed); N/A for the rest given other disqualifiers | Yes | Yes | No | N/A |

---

## 4. Self-managed detail

See §2a. The one material new finding: **Hostinger's VPS line has no plan above KVM8 (8 vCPU /
32 GB)** — `SELENIUM_GRID.md` §5's "Option A: Upgrade to 16 vCPU / 64 GB" implicitly assumed an
in-place resize of the current box. That assumption is wrong; a 16 vCPU/64 GB box is only
available by switching provider entirely (Hetzner or DigitalOcean), which is a full migration
project, not a plan change. This is the single biggest correction this spike makes to the existing
plan — see §6.

## 5. AWS detail

See §2b. Full technical detail (Fargate Spot interruption mechanics, EC2 packing math, Device
Farm's exact disqualifying capability limits, NAT Gateway/data-transfer pricing) is preserved in
the research this table summarizes; the short version is **all AWS paths cost more than the
self-managed alternative at every tier and add ops surface LEM doesn't otherwise carry**, and
Device Farm is a hard no regardless of price.

## 6. Decision

**Nothing hosted is pursued — the whole surveyed market is out.** AWS Device Farm (disqualified —
cannot hold a session), the four QA-testing clouds (BrowserStack, Sauce Labs, LambdaTest,
TestingBot — disqualified or unproven, Sauce Labs explicitly banned in writing), Browserless (wrong
protocol), ZenRows/ScrapingBee (wrong computing model), AWS Fargate/EC2 Grid nodes (technically
fine, but strictly more expensive and more ops-heavy than what's already built and cheaper) — and
also **Steel.dev and Browserbase**, the two that fit LEM's pattern on paper and beat AWS on cost.
Those last two are an owner call rather than a technical disqualification (2026-07-27): the open
questions blocking them (a real Selenium-compatibility spike, and Steel's unreadable ToS) were
judged not worth the engineering time against a self-managed path that is already built, already
cheaper at every tier this plan projects, and already understood operationally. **No vendor spike
is scheduled, at any tier.**

**The path is self-managed, Option B** — additional same-provider Hostinger boxes running
Grid nodes only, cut over via the already-built `docker-compose.grid-node.yml` /
`docker-compose.grid.yml` (issue #556). It is the cheapest option at every tier (~$0–200/mo across
10→100 users), carries zero new vendor/ToS risk, and needs no new capability — just more of what
LEM already runs. `SELENIUM_GRID.md` §5's "Option A" (vertical upgrade) should be treated as
**effectively retired**: the box it assumed doesn't exist on the current provider, and a
cross-provider migration to buy 16 vCPU/64 GB costs more (~$315–504/mo) and carries more one-time
risk (DNS/data/stack cutover) than just adding a second box for the same or lower steady-state
cost.

**Decision trigger:** carried into `docs/scaling-plan.md` §5f — the self-managed Grid cutover
happens per `SELENIUM_GRID.md` §6's existing checklist (capacity monitor breach → re-run the
staggered load test → cut over at parity → then scale nodes/lanes together). That sequence is now
the whole decision; there is no hosted-vendor branch waiting on a spike, and no vendor re-enters it
at the 50- or 100-user tier.

---

## Open questions — only the ones the self-managed path still needs

The hosted-vendor unknowns this spike surfaced (Steel's unreadable ToS, Selenium-vs-CDP
compatibility for Steel/Browserbase, per-user residential-IP stability across days, real
multi-parallel pricing from the sales-gated QA clouds, TestingBot's fair-use ceiling behind
"unlimited", live Fargate/EC2 Spot quotes) are **closed as not-applicable** — the decision above
means none of them gates anything. They're recorded in §2b/§2c so a future reader can see what was
and wasn't verified, not as a to-do list.

What's still genuinely open, on the path actually being taken:

- [ ] Hostinger KVM8 renewal pricing at the point a second box is actually bought — the ~$26–30/mo
      figures above are promotional; renewal is ~$49.99/mo, and Option B's steady-state cost at
      3–4 boxes should be budgeted at renewal rates, not promo.
- [ ] Private networking between two Hostinger boxes: Hetzner offers native unmetered VPC between
      same-region hosts, Hostinger's equivalent wasn't confirmed in this pass. If there isn't one,
      the Grid hub↔node link needs WireGuard, which is a small but real setup item on the Option B
      cutover checklist (`SELENIUM_GRID.md` §6).
- [ ] Hetzner/DigitalOcean US bandwidth allowances remain worth knowing **only** if a
      cross-provider move is ever reconsidered for reasons other than Selenium capacity (Hetzner's
      US allowance dropped to 1 TB/mo vs 20 TB/mo in EU alongside its June 2026 price increase).

---

## Sources

**AWS:** aws.amazon.com/fargate/pricing · aws.amazon.com/device-farm/faqs ·
docs.aws.amazon.com/devicefarm/latest/testgrid/techref-support.html ·
economize.cloud/resources/aws/pricing/ec2/c5.2xlarge · aws.amazon.com/vpc/pricing ·
egresscost.com/aws/data-transfer-pricing · github.com/taktakpeops/selenium-grid-ecs-fargate ·
code.mendhak.com/selenium-grid-ecs (all accessed 2026-07-27)

**Self-managed:** hostinger.com/vps-hosting · digitalocean.com/pricing/droplets ·
northflank.com/blog/hetzner-cloud-server-price-increases ·
wz-it.com/en/blog/hetzner-price-increase-june-2026-cpx-ccx-alternatives (all accessed 2026-07-27)

**Commercial vendors:** browserstack.com/terms · browserstack.com/support/faq/automate ·
saucelabs.com/doc/acceptable-use-policy · saucelabs.com/pricing · testmuai.com/pricing ·
testmuai.com/legal/aup · testingbot.com/pricing · testingbot.com/terms · browserless.io/pricing ·
browserless.io/terms-of-service · docs.steel.dev/overview/pricinglimits ·
docs.steel.dev/overview/profiles-api/overview · steel.dev/blog/dedicated-ips ·
docs.browserbase.com/platform/browser/core-features/contexts ·
docs.browserbase.com/platform/identity/proxies · browserbase.com/terms-of-service ·
docs.zenrows.com/first-steps/pricing · docs.zenrows.com/forbidden-sites ·
scrapingbee.com/acceptable-use-policy (all accessed 2026-07-27)

**LinkedIn ToS context:** linkedin.com/help/linkedin/answer/a1341387 (accessed 2026-07-27)
