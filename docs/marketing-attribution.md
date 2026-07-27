# Marketing attribution — UTM discipline, conversion goals, channel reporting

Issue #658. Companion to `docs/kpi-dashboards.md` (the KPI surface), `docs/launch-and-marketing-plan.md`
§C.5 (the plan this implements) and `docs/error-tracking.md` / `docs/llm-analytics.md` (the other two
PostHog streams).

## The gap this closes

#503 built the **capture** half of the funnel: `signup_started` → `signup_completed` →
`trial_started` → `activated` → `subscription_started`, each carrying whatever UTMs the browser
landed with, plus a derived `channel`. It works — and it reported almost everything as `direct`,
because nothing tagged the links LEM publishes. A brand post CTA, a carried first comment, a YouTube
description and a newsletter subscribe link all sent traffic to a bare URL, so the capture side had
nothing to read.

This closes the loop at the **source**: one helper, applied at every surface that publishes a link.

## The one helper

`src/cqc_lem/utilities/marketing/attribution.py`. Two rules make it safe to call everywhere:

- **Only OUR OWN destinations are tagged.** `is_owned_link()` gates every call. UTMs are query
  params the destination's analytics reads — stamping them on a Reuters article we cited pollutes
  someone else's reporting and attributes nothing to us. The owned set is `PUBLIC_BASE_URL`,
  `BRAND_SIGNUP_URL` and anything in the new `MARKETING_OWNED_DOMAINS` env (comma-separated, bare
  host or full URL), plus their subdomains. With none of them configured **nothing is tagged** —
  the pre-#658 behaviour exactly.
- **An existing UTM is never overwritten.** `build_utm_url` only fills in what is missing, so a
  hand-tagged link keeps its own attribution and running the helper twice on one URL is a no-op.
  That is what lets more than one choke point tag the same link safely.

`mark_placement()` is the single deliberate exception: it REPLACES `utm_content`, and only that.
A promo CTA is written days before publish and assumes its link stays in the post body; #392's split
decides at publish time whether the link is carried into the first comment instead. Placement is a
fact observed at publish, not a guess made at generation.

### Vocabulary

| Param | Values | Meaning |
|---|---|---|
| `utm_source` | `linkedin` · `newsletter` · `youtube` · `referral` · `email` | Where the link was published |
| `utm_medium` | `social` · `video` · `newsletter` · `profile` · `referral` | The marketing medium |
| `utm_campaign` | `post-<id>[-<archetype>]` · `newsletter-<id>` · `tutorial-<flow>` · `brand-profile` · `member-referral` | The publishable ITEM |
| `utm_content` | `post_body` · `first_comment` · `video_description` | Placement within the medium |
| `ref` | `<user id>` | The referral link's referrer — a PERSON, not a creative variant |

Campaigns come from `campaign_for_post` / `campaign_for_edition` / `campaign_for_tutorial`, never
spelled at a call site: a breakdown by campaign is only readable if every post writes it the same way.

## Where it is applied

| Surface | Call site | Campaign |
|---|---|---|
| Promo CTA in a post body | `content_alignment.artifact_cta_line` | `post-<id>` (`utm_content=post_body`) |
| Link carried into the first comment | `content_alignment.first_comment_link_text` | `post-<id>` (`utm_content=first_comment`) |
| Brand account's seeded goal URL | `brand_account.brand_preference_overrides` | `brand-profile` |
| YouTube tutorial description | `video_tutorials.description_with_cta` | `tutorial-<flow key>` |
| Newsletter edition body | `run_automation._tagged_edition_body` | `newsletter-<edition id>` |

**A LinkedIn newsletter EDITION carries no outbound links by design** — the generator prompt forbids
them, because an off-platform link suppresses an article's reach. So on the mainline path the edition
tagger is a no-op. It exists because the SPA lets an author edit a draft before it publishes, and a
hand-added link to their own site is exactly the traffic worth attributing to the edition that sent
it. Publish time is the right choke point: the last moment the body is still ours, and both publish
tasks pass through it.

DM templates are deliberately NOT tagged. A template's links are the user's own text, not a link LEM
generated, and rewriting someone's copy is a different decision from tagging our own.

## The capture side

Unchanged in shape, extended in two places:

- **`ref` is allow-listed** (`observability._ATTRIBUTION_KEYS`, `api.main.FunnelAttribution`,
  `ui/src/utils/attribution.ts`). A `ref` with no UTMs still resolves to the `referral` channel — a
  member who pastes their link into a DM routinely loses the rest of the query string.
- **`youtube` is a channel.** YouTube passes no usable referrer, so without its own bucket every
  video-driven signup landed in `other`.

`track_funnel_event` already writes first touch onto the PostHog person as `initial_<key>` via
`$set_once`. The SPA now writes the SAME keys (`analytics.identifyUser` / `recordSignup`), so browser
and backend converge on one set of person properties instead of two half-populated ones. That is what
makes `activated` — a Celery-side event that knows no UTMs — attributable at all.

`recordSignup()` fires `signup_completed_web` from the browser on a brand-new account only.
**It is not the same event as the API's `signup_completed`, and the two must never be summed:** one
signup produces both. The server event is the funnel of record; the browser event is what PostHog web
analytics can resolve to a session, which is what a conversion goal needs.

## Conversion goals and the channel dashboard

`scripts/posthog_provision.py` owns both, same `--dry-run` / `--apply` contract as everything else in
that script.

- **Conversion goals** are provisioned as PostHog **actions** (that is what web analytics picks a
  goal from): *Signup completed* → `signup_completed_web`, *Activated* → `activated`. The signup goal
  reads the BROWSER event on purpose — pointing it at the server event would report every signup as
  an unattributed conversion, since a server event has no session or pageview context.
- **LEM Channels** is the third dashboard: signups by source/campaign/placement, the weekly channel
  mix, activations by first-touch channel, the brand-post → visit → signup funnel (first step scoped
  to `utm_source=linkedin`), referral signups by `ref`, and tagged landing visits.

The goal diff compares only each step's EVENT: PostHog hydrates a step with its own id and a pile of
nulls, and matching whole objects would rewrite an action nobody touched on every run. The personal
API key needs `action:read` + `action:write` on top of the existing scopes.

## Verifying end to end

1. `MARKETING_OWNED_DOMAINS` and `BRAND_SIGNUP_URL` are set in the environment. Without them nothing
   is tagged and every tile reads `(none)` — that row against a campaign you are running is the tell.
2. `poetry run python scripts/posthog_provision.py --dry-run` → the Channels dashboard and both goals
   appear as pending; `--apply` converges them.
3. Open a brand post's CTA link. `$pageview` should carry `utm_source=linkedin` and the post campaign.
4. Complete a signup on that session. `signup_completed_web` (browser) and `signup_completed` (API)
   both land, the person gains `initial_utm_source` / `initial_channel`, and the row appears on
   *Channels — Signups by source and campaign*.

Paid-ads marketing analytics is explicitly out of scope until paid acquisition exists, and the
retired revenue dashboard (gone 2026-06-30) is not used anywhere here.
