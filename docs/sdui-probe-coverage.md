# SDUI surface inventory + probe coverage matrix

Issue #1013. Every Selenium touchpoint in `app/engagement/*` (the feed, group and roster walks, the
newsletter rail, the connect rail, the publish/sweep lane and the DM/outreach lane all moved there
in #1154, and `app/run_automation.py` was deleted in #1206) and `utilities/linkedin/*`, the
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
| Home feed "Sort by → Recent" | `_switch_feed_to_recent` | `--feed-sort` | yes | `feed_sort` on the funnel + `feed_scan` (#817). A home feed that rendered cards and still resolved no sort control also ships a bounded DOM sample as `sdui_selector_evidence` (`surface='feed_sort_control'`) — the same two-pass scan the comment sweep uses (`utilities/linkedin/sort_evidence.py`), shipped as an EVENT so prod's log filters cannot drop it (#1270). Nothing is emitted off the home feed or on a feed the zero-walk cross-check says rendered nothing |
| Feed card walk + reactions | `_card_for_textbox` / `react_to_post_inline` | `--reaction-probe` | yes | `feed_walk` / `cards_seen` (and `textboxes_seen`) on the funnel — zero markers is cross-checked against the REACTION control (`_FEED_WALK_CROSSCHECK_SEL`), never against a marker the walk already counts; `no_text` (image/video-only cards) and `not_walked` (budget spent / deadline passed) are DEBUG, only `drift` warns (#1013/#1081) |
| Feed share-box composer | `_post_composer_for_card` | `--probe-composer` | yes | per-card miss is a DEBUG no-op by design (#876) |
| Profile-views viewer list | `_PROFILE_VIEWER_ROWS_JS` | `--profile-views` | yes | zero rows vs the page's headline stat (#1009) |
| Profile header scrape + degree badge | `parse_profile_header` / `_profile_is_first_degree` | `--profile-scrape` | yes | no name vs the page's `/in/` links; no badge vs the page's own degree LINE (#1021) |
| Profile experience rows (`/details/experience/`) | `parse_profile_experiences` | `--profile-experiences` | yes | dated rows the parser cannot read is drift; an entity with no date range yields nothing rather than a guessed company (#970); every role parsed and NONE attributed is drift too, some blank is not (`experiences_without_company`, #1096); `dated_line_containers` names the ancestor chain each dated line actually sits in, so a render with no markup vocabulary is readable from the first report (#1465) |
| Connect invite dialog | `_open_connect_invite_dialog` | `--connect-dialog` | no (needs a target) | dialog controls must be present before Send (#1012); a missing note affordance is graded against the bare-send control, never warned (#1039) |
| Catch-up moment cards | `_CATCHUP_CARD_LOCATORS` | `--catchup-cards` | yes | zero cards vs `main div[role='listitem']` (#1013) |
| Group share box / editor | `auto_post_to_group` | `--group-composer` | no (needs a group) | `_unpostable` rotates past the group (#858) |
| Groups directory + a group's membership controls | `_enumerate_joined_groups` / `auto_comment_in_groups` | `--group-membership` | yes | directory anchors the sync matched none of, or that no section heading could be attributed to (the reading that says whether the sync counts recommendation cards as joins — unanswered is drift, never `ok`); an enumerated id sitting under a recommendation heading (#1316); a blind zero-walk tripwire — `_GROUPS_DIRECTORY_CROSSCHECK_SEL` matching nothing on a directory that rendered group anchors, which would make an empty sync grade as a quiet day; a group header carrying no join/leave control (#1052) |
| Group feed post card → Comment → inline composer | `_post_composer_for_card` / `_single_post_scope` | `--group-feed-composer` | yes | per-card miss is a DEBUG no-op by design, so the walk itself is the tripwire: posts the page renders that the card walk reached none of, or a composer mounted on the page that the card-scoped resolver claimed for no card, is drift; the home feed is the control (#916/#928) |
| Company-page invite modal | `automate_invitations` | `--company-invite` | yes | zero ticked boxes vs the picker's own rows → `drift` ≠ `no_candidates` (#1021) |
| Invitation manager → Sent | `read_pending_invites` | `--sent-invites` | yes | zero rows vs the page's own empty-state copy (#969) |
| Roster activity Follow control | `_resolve_follow_control` | `--roster-follow` | no (needs a target) | `unknown` clicks nothing; blocked visits recorded (#962) |
| Roster activity connection state | `_resolve_connect_state` | `--roster-connect` | no (needs a target) | `unknown` never escalates; read-only advancement only moves forward (#979) |
| Recommendations + mentions | `_RECOMMENDATION_CARD_LOCATORS` / `_MENTION_CARD_LOCATORS` | `--appreciation-sources` | yes | undated card is SKIPPED, never thanked (#968); zero recommendation cards vs the page's own "Month D, YYYY" (`page_dated`, #1007) and zero mention cards vs the page's own "mentioned/tagged you" lines (`page_mentions`, #1374) — either way a zero the page contradicts is `drift`, not `unknown` |
| Newsletter/article editor | `find_article_editor_elements` | `--article-editor-url` | yes | `editor_ready` gates the publish walk (#771/#804) |
| Newsletter page (subscriber label + edition list) | `_read_newsletter_subscriber_count` | `--newsletter-url` | no (needs a newsletter URL) | the page's own "N subscribers" text vs a `None` from the reader → every `track_newsletter_subscribers` snapshot writes NULL and the growth series flatlines (#1284). Repeatable: it also reads a THIRD-PARTY newsletter, which is how an editorial audit names a real exemplar |
| Published newsletter edition (article body) | n/a — editorial evidence only | `--newsletter-edition` | no (needs an edition URL) | never `drift`: it grounds no production chain, it samples a real exemplar's hook/structure/CTA for `docs/content-quality-audits/newsletter.md` (#1284) |
| Own post detail + analytics counts | `_post_social_counts` | `--post-url` | no (needs a post) | all-zero vs a non-zero count beside the page's own label; drift leaves the post uncaptured (#1021) |
| Post media render (document vs image) | media anchors | `--post-url` | no (needs a post) | n/a — a diagnostic, not a lane |
| Comment thread + sort | `_comment_items` / `_switch_comment_sort` | `--comment-outcome-url` | no (needs a post) | `visible_most_relevant` is three-valued; NULL excluded (#628). A thread that rendered but yielded no sort control also ships a bounded DOM sample as `sdui_selector_evidence` (`surface='comment_sort_control'`, with the `post_url` to re-run the probe against) — an EVENT, because prod's `LOG_LEVEL=INFO` / `POSTHOG_LOG_LEVEL=WARNING` drops the DEBUG line that carried it before (#1117). That scan is ALSO the level gate AND the visibility gate: grounded 2026-08-14, a short thread has no sort control at all, so only a scan row that still NAMES a sort (`reason='keyword'`) warns and only such a row keeps the reading NULL — everything else is DEBUG, and a comment found there records `visible_most_relevant=1` unless the scan came back BLIND (`[]` is equally a failed read, so it stays NULL) (`docs/sdui-selenium-notes.md`). The probe reads the label silently for the same reason |
| Comment card author identity | `comment_author_identity` | `--commenter-read` | yes (own freshest post; a URL is optional) | it reads each card BOTH ways — the naive first `a[href*='/in/']` and the header anchor — and grades the SHIPPED read: `drift` when the header read names nobody, and the gap against the naive read is drift only on an image that still ships it (`reader_source='script'`), because after #1091 that gap is the fix working and would otherwise file weekly. That gap is #1091: the avatar anchor (no text) and an @mention in the body are both `/in/` links, so the naive read named nobody, `upsert_engager` was skipped in silence and `post_engagers` recorded nothing for a month while replies on the same cards kept landing |
| Message-thread ladder | `open_message_thread` | `--dm-thread-url` | no (needs a target) | `ThreadState.UNKNOWN` skips (#731) |
| Post permalink card → Comment → composer | `_permalink_post_card` / `_post_composer_for_card` | `--permalink-comment` | no (needs a post) | a comment that does not land is a FAILURE row, never SUCCESS (#966) |

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
   The sweep checks the session FIRST (`sweep_session_state`): a signed-out one renders an auth wall
   at every surface, which reads as `drift` on nearly all of them at once, so it grades every probe
   `unknown` and probes nothing. That check fails OPEN — only LinkedIn's own challenge URL or guest
   copy stands the sweep down, never an unreadable page.
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

The report is printed between `===LEM-PROBE-JSON-BEGIN===` / `===LEM-PROBE-JSON-END===` fences,
because the probe does NOT own its stdout: it runs inside the worker, where
`cqc_lem.utilities.logger` writes there too (`get_current_profile` logs before the first probe).
`sdui_drift_issues.fenced_report` is the ONE place that capture is cut back down to the report —
an ad-hoc second parse would fail on the first log line and read as "no drift".

The sweep is read-only in the way that matters: it sends no invite, posts nothing, comments on
nothing, ticks no checkbox, and clicks no Send / Post / Invite control. The composer probes open a
composer and close it with Escape without typing.

Since #1301 that is **enforced, not intended**: `install_read_only_guard()` patches Selenium once
the session is up, so no probe can type a printable character (every LinkedIn write starts there)
or press a control whose label commits something — via `WebElement.click`, `ActionChains` or
`arguments[0].click()` alike. Two gates run before the browser opens: the 429 breaker / automation
pause, read **fail-CLOSED** (unreadable refuses), and the Grid debug-node pin, so a probe never
takes one of the eight Chrome slots the lanes are sized for. A refusal exits **75** — a WAIT, which
the weekly cron logs and does not alert on. That is what lets a pipeline agent run the probe
itself; the conditions are in the **linkedin-live-validation** skill. One route is deliberately
un-probeable as a result: the message-thread ladder's messaging-SEARCH fallback types a name and
presses Enter, so it grades `unknown` naming the guard.

## Gaps (tracked, not silent)

Every walk in the matrix now carries a production tripwire (#1021 closed the last three). What is
deliberately left, and why:

- **Connect-dialog note affordance** — a missing `Add a note` button is the expected quota-spent
  fallback (`_add_connect_note` sends the invite bare and logs DEBUG, #1039) and is also what a
  rotated label would look like, and production cannot tell them apart: the dialog renders exactly
  the same way once a free account's personalized invites are spent. Warning on it filed a
  fingerprinted defect per lost note, so the reading moved to the probe — `--connect-dialog` reports
  `note_affordance_present` / `bare_send_present` and says so in its verdict. It is deliberately
  NOT graded `drift`: the probe account's own quota state is not knowable from the page. This is the
  one reading that exists only when somebody runs the probe, since `--connect-dialog` needs a target
  and so is not in the weekly sweep.
- **Feed share-box composer** — a per-card composer miss stays a DEBUG no-op (#876). The card walk
  above it is what has the tripwire; warning per card would file a defect for a post that legitimately
  renders no composer.
- **Post media render** — a diagnostic, not a lane. Nothing in production reads it, so there is no
  zero to misread.
- **Target-needing surfaces** (`--connect-dialog`, `--group-composer`, `--roster-follow`,
  `--comment-outcome-url`, `--dm-thread-url`) are not in the weekly sweep, because each needs a URL a
  human picks. Their production paths fail CLOSED instead (`unknown` skips, a blocked visit is
  recorded), which is the tripwire in a shape a sweep cannot provide.

The tripwires and the sweep answer different questions and both are kept: the sweep grades a surface
once a week from a read-only session; the tripwire grades the read production actually made, per run.

### The one thing a tripwire cannot do

A tripwire says a locator went blind; it cannot say what the replacement is. That still takes a live
`--profile-scrape` / `--company-invite` run and the `degree_anchors` / `page_text` it hands back —
which is why every re-grounding PR carries `risk:live-linkedin` and merges by the owner.
