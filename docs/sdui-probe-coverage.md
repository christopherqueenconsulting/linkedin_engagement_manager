# SDUI surface inventory + probe coverage matrix

Issue #1013. Every Selenium touchpoint in `app/run_automation.py` and `utilities/linkedin/*`, the
read-only probe flag that grounds it, and the production-path tripwire that stops a zero result
from reading as "nothing to do".

Read this with `docs/sdui-selenium-notes.md` (what the live DOM actually looks like) and the
**linkedin-live-validation** skill (who may run a probe, and how).

## Why this exists

Three surfaces were found dead-or-dangerous within days in August 2026, each silently:

| | What broke | How long it was invisible |
|---|---|---|
| #964 | Catch-up card locators never matched the SDUI DOM | `no_moments` daily while the feed showed ten |
| #1009 | `//ul[@aria-label="List of Entities"]` gone | every run engaged 0 viewers, for weeks |
| #1012 | Unscoped `Invite ` locator matched the suggestion rail | ~20 invites sent to **random people** |

One failure shape: pre-SDUI anchors (`<ul>`/`<li>`, `artdeco-*`, rotated aria-labels), unscoped
matches, and success measured as "a click landed" instead of "the outcome is provably present".

## The matrix

`scripts/linkedin_live_validation.py --surfaces` prints this as JSON — that is the source of truth
(`SURFACES`), and unit tests fail the build if a row here has no CLI flag, or if a surface in the
code is missing from this table.

| Surface | Code | Probe flag | In the weekly sweep | Production tripwire |
|---|---|---|---|---|
| Home feed "Sort by → Recent" | `_switch_feed_to_recent` | `--feed-sort` | yes | `feed_sort` on the funnel + `feed_scan` (#817) |
| Feed card walk + reactions | `_card_for_textbox` / `react_to_post_inline` | `--reaction-probe` | yes | `feed_walk` / `textboxes_seen` on the funnel (#1013) |
| Feed share-box composer | `_post_composer_for_card` | `--probe-composer` | yes | per-card miss is a DEBUG no-op by design (#876) |
| Profile-views viewer list | `_PROFILE_VIEWER_ROWS_JS` | `--profile-views` | yes | zero rows vs the page's headline stat (#1009) |
| Profile header scrape + degree badge | `parse_profile_header` / `_profile_is_first_degree` | `--profile-scrape` | yes | **none — see Gaps** |
| Connect invite dialog | `_open_connect_invite_dialog` | `--connect-dialog` | no (needs a target) | dialog controls must be present before Send (#1012) |
| Catch-up moment cards | `_CATCHUP_CARD_LOCATORS` | `--catchup-cards` | yes | zero cards vs `main div[role='listitem']` (#1013) |
| Group share box / editor | `auto_post_to_group` | `--group-composer` | no (needs a group) | `_unpostable` rotates past the group (#858) |
| Company-page invite modal | `automate_invitations` | `--company-invite` | yes | **none — see Gaps** |
| Invitation manager → Sent | `read_pending_invites` | `--sent-invites` | yes | zero rows vs the page's own empty-state copy (#969) |
| Roster activity Follow control | `_resolve_follow_control` | `--roster-follow` | no (needs a target) | `unknown` clicks nothing; blocked visits recorded (#962) |
| Recommendations + mentions | `_RECOMMENDATION_CARD_LOCATORS` / `_MENTION_CARD_LOCATORS` | `--appreciation-sources` | yes | undated card is SKIPPED, never thanked (#968) |
| Newsletter/article editor | `find_article_editor_elements` | `--article-editor-url` | yes | `editor_ready` gates the publish walk (#771/#804) |
| Own post detail + analytics counts | `_post_social_counts` | `--post-url` | no (needs a post) | **none — see Gaps** |
| Post media render (document vs image) | media anchors | `--post-url` | no (needs a post) | n/a — a diagnostic, not a lane |
| Comment thread + sort | `_comment_items` / `_switch_comment_sort` | `--comment-outcome-url` | no (needs a post) | `visible_most_relevant` is three-valued; NULL excluded (#628) |
| Message-thread ladder | `open_message_thread` | `--dm-thread-url` | no (needs a target) | `ThreadState.UNKNOWN` skips (#731) |

**Not a Selenium surface**, so deliberately not in the matrix: post publishing and document upload
(`/rest/posts`, `/rest/documents` — grounded by `scripts/linkedin_version_check.py`), token refresh,
and email-PIN verification.

## The three-state verdict

Every probe grades itself into exactly one of three states, next to its prose `verdict`. Only
`drift` becomes an issue, and it is only ever claimed against a **page-native cross-check** — a
headline count, the page's own empty-state copy, or an anchor the chain should have matched.

| State | Means | What happens |
|---|---|---|
| `ok` | The chain resolved what production needs | nothing |
| `drift` | The PAGE shows content the locator cannot see | ONE deduped `agent:ready` issue |
| `unknown` | The page did not render, or the surface is legitimately absent | reported, **never filed** |

`unknown` is not a soft `drift`. A page that did not render grounds nothing, and filing it would
put the same non-finding in the backlog every Monday until it buried the real drift underneath —
which is the same mistake as reading "nothing found" as "nothing there", pointed the other way.

Where a run cannot grade a half of its surface it says so rather than passing: `--profile-scrape`
against your OWN profile reports `degree_grounded: false`, because your own profile carries no
degree badge and a green verdict there would claim coverage the run does not have.

## The weekly drift cron

`scripts/weekly_sdui_drift_check.sh` (host cron, as `lem`, Mondays 06:40 UTC):

```cron
40 6 * * 1 /home/lem/<repo-clone>/scripts/weekly_sdui_drift_check.sh
```

1. Runs `linkedin_live_validation.py --sweep` inside `celery_worker_selenium` — every target-free
   probe, ONE Chrome session, off-peak. A probe that raises is recorded `unknown` and the sweep
   continues; one rotated surface must not cost the reading of the nine that did not rotate.
2. `scripts/sdui_drift_issues.py --apply` files ONE `agent:ready` issue per `drift`, with the probe
   JSON, the reproduce command and real acceptance criteria. Labels: `agent:ready`, `bug`,
   `priority:high`, `risk:live-linkedin` (re-grounding cannot be verified without a live run, so the
   merge belongs to the owner).
3. Dedup is the probe key (`sdui-drift-<key>` in the body) against **OPEN** issues only. Unlike the
   PostHog error filer, a CLOSED issue does not suppress a re-file: a surface that rotted, got
   re-grounded, and rotted again six months later is a new defect.

Env overrides: `SDUI_PROBE_CONTAINER`, `SDUI_PROBE_USER_ID`, `SDUI_PROBE_PROFILE_URL` (a
2nd/3rd-degree profile, so the degree badge is actually grounded), `SDUI_DRIFT_DIR`,
`SDUI_DRIFT_REPO`, `DRY_RUN=1`.

The sweep is read-only in the way that matters: it sends no invite, posts nothing, comments on
nothing, ticks no checkbox, and clicks no Send / Post / Invite control. The composer probes open a
composer and close it with Escape without typing.

## Gaps (tracked, not silent)

Three production paths still lack a zero-result tripwire. They are named here rather than left to
be rediscovered:

- **Profile header scrape** — `parse_profile_header` raises `ProfileUnavailableError` on an error
  page, but a page that renders with no `<h1>` and no usable `<title>` yields no name and no
  cross-check. The degree badge has no tripwire at all: `_profile_is_first_degree` fails OPEN, and
  `profile.is_1st_connection` (which routes every profile viewer down the DM/comment branch) reads
  the same dead `span.dist-value` anchor.
- **Company-page invite modal** — `select_connection_checkboxes` returning zero is reported as
  `no_candidates`, indistinguishable from a rotated invitee-row XPath.
- **Own post stats** — `_post_social_counts` scoring every signal 0 is indistinguishable from a post
  with no engagement.

The probes above grade all three today, so the weekly sweep catches them; the tripwires would catch
them in production, per run, which is stronger.
